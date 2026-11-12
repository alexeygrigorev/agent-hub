"""Managed Claude release re-pin through the real Hub2 updater transaction.

Synthetic only: no network, service, provider, credential or live profile. The
updater's helper subprocess is dispatched in-process to the actual helper with
the exact argv, environment and interpreter it would receive; only the Hub
transport is a fake bound to the synthetic enrolled host.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import zipfile
from unittest.mock import patch

import pytest

from agent_hub_host import (bus, config, credentials, engine_login, managed_claude_repin as repin,
                            native_import_host, provisioning as p)

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='release-pinned managed Claude is Linux only')

REPO = Path(__file__).resolve().parents[2]
NEW = 'a' * 40
OLD = 'b' * 40
REV = '33333333-3333-4333-8333-333333333333'
OTHER_REV = '11111111-1111-4111-8111-111111111111'
PATH = '.claude/.credentials.json'
SECRET = 'synthetic-credential-marker'
CREDENTIAL = json.dumps({'claudeAiOauth': {'accessToken': SECRET, 'refreshToken': '',
                                             'expiresAt': int(time.time() * 1000) + 86400000}})
GENERATION = 7
MACHINE = 'repin-test-machine'
BASE = 'https://example.invalid'
STATUS_PATH = '/api/fleet/routing/self'
WHEEL = f'agent_hub_host_v2-2.0.0+g{NEW}-py3-none-any.whl'
OPERATOR_BEFORE = 'version = 1\n\n# operator comment kept byte-for-byte\n[profiles.operator]\nengine = "shell"\ncommand = ["sh"]\n'
OPERATOR_AFTER = '\n[profiles.after]\nengine = "shell"  # trailing operator table\n'


def load_updater():
    spec = importlib.util.spec_from_file_location('hub2_self_update_repin', REPO / 'scripts/hub2-self-update.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


updater = load_updater()


def archive_bytes(marker):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('marker', marker)
    return buffer.getvalue()


def target_bundle(**changes):
    value = dict(label='hub-claude', provider='claude', version='2.1.39', revision=REV,
                 credential_generation=GENERATION, credential_paths=[PATH], files={PATH: CREDENTIAL},
                 env={}, args=[])
    value.update(changes)
    return value


def other_bundle():
    return dict(label='test-account', provider='codex', version='0.163.0', revision=OTHER_REV,
                files={}, env={}, args=[])


@contextlib.contextmanager
def as_release(release):
    """The interpreter/archive identity a release's own helper runs with."""
    with patch.object(sys, 'executable', str(release / 'bin/python')), \
         patch.object(sys, 'argv', [str(release / 'hub.pyz')]):
        yield


class World:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.monkeypatch = monkeypatch
        self.home = tmp_path / 'home'
        self.root = (tmp_path / 'agent-hub2').resolve()
        self.releases = self.root / 'releases'
        self.managed = tmp_path / 'managed'
        self.profile = tmp_path / 'aplexer' / 'config.toml'
        monkeypatch.setenv('HOME', str(self.home))
        monkeypatch.setenv('APLEXER_CONFIG', str(self.profile))
        monkeypatch.setenv('AGENT_HUB_MANAGED_DIR', str(self.managed))
        # Worker selectors that must never reach the helper.
        monkeypatch.setenv('HUB2_CONFIG', str(tmp_path / 'worker-selector-never-read.json'))
        monkeypatch.setenv('APLEXER_TAG', 'synthetic-tag')
        monkeypatch.setenv('APLEXER_WORKSPACE', str(tmp_path))
        self.enrollment = self.home / '.config/agent-hub/v2.json'
        self.enrollment.parent.mkdir(parents=True, mode=0o700)
        self.enrollment.write_text(json.dumps({'machine_id': MACHINE, 'server': BASE, 'token': 'synthetic-enrollment'}))
        self.enrollment.chmod(0o600)
        self.files = {name: ('artifact:' + name).encode() for name in
                      (WHEEL, 'websockets-16.0-py3-none-any.whl', 'hub2-self-update.py', 'hub2-enroll.py',
                       'host-install.sh', *updater.UNITS)}
        self.files['hub.pyz'] = archive_bytes('new release')
        self.manifest = self.release_manifest(NEW, self.files)
        self.hub = {'generation': GENERATION, 'revision': REV}
        self.observation = None  # an Exception models a non-2xx Hub refusal
        self.acks = []  # per-ACK responses; default recorded
        self.on_ack = None
        self.hub_calls = []
        self.helper_runs = []
        self.helper_outputs = []
        self.commands = []
        self.helper_hook = None

    def release_manifest(self, version, files):
        return {'schema_version': 1, 'protocol_version': 1, 'host_version': version, 'python_requires': '>=3.11',
                'artifacts': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
                              for name, data in files.items()}}

    def make_release(self, version, marker):
        release = self.releases / version
        (release / 'bin').mkdir(parents=True)
        (release / 'bin/python').write_text('synthetic interpreter, never executed\n')
        data = archive_bytes(marker)
        (release / 'hub.pyz').write_bytes(data)
        files = dict(self.files, **{'hub.pyz': data})
        manifest = self.release_manifest(version, files)
        (release / 'release.json').write_text(json.dumps(manifest))
        return release

    def command_for(self, release):
        with as_release(release):
            return p.module_command('execute', 'hub-claude')

    def configure_target(self, release):
        """An installed managed Claude target pinned to ``release`` plus unrelated state."""
        self.managed.mkdir(mode=0o700)
        rows = [{'label': 'test-account', 'revision': OTHER_REV, 'provider': 'codex'},
                {'label': 'hub-claude', 'revision': REV, 'provider': 'claude'}]
        (self.managed / 'state.json').write_text(json.dumps(rows, indent=1) + '\n')
        (self.managed / 'state.json').chmod(0o600)
        unrelated = self.managed / ('test-account-' + OTHER_REV)
        unrelated.mkdir(mode=0o700)
        (unrelated / 'keep.bin').write_bytes(b'unrelated exact bytes')
        dest = self.managed / ('hub-claude-' + REV)
        (dest / 'home/.claude').mkdir(parents=True, mode=0o700)
        dest.chmod(0o700)
        (dest / 'home').chmod(0o700)
        p.write_json(dest / 'bundle.json', target_bundle())
        credential = dest / 'home' / PATH
        credential.write_text(CREDENTIAL)
        credential.chmod(0o600)
        p.write_json(dest / credentials.STATE_FILE,
                     {'generation': GENERATION, 'digests': {PATH: hashlib.sha256(CREDENTIAL.encode()).hexdigest()}})
        self.profile.parent.mkdir(mode=0o700)
        self.profile.write_text(OPERATOR_BEFORE)
        self.profile.chmod(0o600)
        with as_release(release):
            p.profiles([other_bundle(), target_bundle()])
        with self.profile.open('a') as stream:
            stream.write(OPERATOR_AFTER)
        self.snapshot = self.bytes()

    def bytes(self):
        """Every byte the re-pin must preserve (profile handled separately)."""
        result = {}
        for path in sorted(self.managed.rglob('*')):
            if path.is_file() and path.name != 'reconcile.lock':
                result[str(path.relative_to(self.managed))] = path.read_bytes()
        return result

    def pinned(self):
        import tomllib
        return tomllib.loads(self.profile.read_text())['profiles']['hub-hub-claude']['command']

    def repinned_text(self, original, old, new):
        line = 'command = ' + json.dumps(self.command_for(old))
        assert original.count(line) == 1
        return original.replace(line, 'command = ' + json.dumps(self.command_for(new)))

    # Fake transport bound to the synthetic enrolled host.
    def request(self, method, path, payload=None):
        self.hub_calls.append((method, path, payload))
        match = re.fullmatch(r'/api/engines/hub-claude/generation\?generation=(\d+)&revision=([a-f0-9-]{36})', path)
        if method == 'GET' and match:
            if isinstance(self.observation, Exception):
                raise self.observation
            if (int(match.group(1)), match.group(2)) != (self.hub['generation'], self.hub['revision']):
                raise RuntimeError('synthetic HTTP 409')
            return {'label': 'hub-claude', 'generation': self.hub['generation'], 'revision': self.hub['revision'],
                    'status': 'current'}
        if (method, path) == ('POST', '/api/engines/sync-report'):
            item = payload['items'][0]
            if (item['credential_generation'], item['revision']) != (self.hub['generation'], self.hub['revision']):
                raise RuntimeError('synthetic HTTP 409')
            index = sum(1 for call in self.hub_calls if call[1] == '/api/engines/sync-report') - 1
            if self.on_ack:
                self.on_ack(index)
            result = self.acks[index] if index < len(self.acks) else {'status': 'recorded'}
            if isinstance(result, Exception):
                raise result
            return result
        pytest.fail('unexpected Hub request ' + method + ' ' + path)

    def bind(self, base):
        assert base == BASE
        return MACHINE, self.request

    # Updater process boundary.
    def command(self, args):
        args = [str(value) for value in args]
        self.commands.append(args)
        if 'venv' in args:
            target = Path(args[-1])
            (target / 'bin').mkdir(parents=True, exist_ok=True)
            (target / 'bin/python').write_text('synthetic interpreter, never executed\n')

    def run(self, args, **kwargs):
        if args[3:5] == ['-c', updater.REPIN_BOOT]:
            return self.run_helper(args, **kwargs)
        assert args[-3:] == ['request', 'GET', STATUS_PATH]
        status = {'machine_id': MACHINE, 'connected': True, 'host_version': Path(args[0]).parents[1].name,
                  'protocol_version': 1, 'version_mismatch': False}
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(status).encode())

    def run_helper(self, args, *, stdin, stdout, stderr, timeout, env, cwd, pass_fds):
        # Recovery-capable updater code is durable before the helper can publish.
        assert (self.root / 'updater.py').read_bytes() == self.files['hub2-self-update.py']
        archive = Path(args[5])
        release = archive.parent
        assert args[:3] == [str(release / 'bin/python'), '-I', '-S']
        assert args[6:] == ['repin', BASE, str(self.root / updater.REPIN_JOURNAL)]
        assert (stdin, stdout, stderr, timeout) == (subprocess.DEVNULL, subprocess.PIPE, subprocess.DEVNULL, 120)
        assert not {'HUB2_CONFIG', 'HUB_CONFIG_FILE', 'APLEXER_TAG', 'APLEXER_WORKSPACE'} & set(env)
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == json.loads(
            (release / 'release.json').read_text())['artifacts']['hub.pyz']['sha256']
        self.helper_runs.append(release.name)
        output = io.StringIO()
        with patch.dict(os.environ, env, clear=True), patch.object(sys, 'executable', args[0]), \
             contextlib.redirect_stdout(output):
            code = repin.main(args[5:])
        self.helper_outputs.append(json.loads(output.getvalue()))
        assert SECRET not in output.getvalue()
        if self.helper_hook:
            self.helper_hook()
        return subprocess.CompletedProcess(args, code, stdout=output.getvalue().encode())

    def fetch(self, url, limit):
        name = url.rsplit('/', 1)[1]
        return json.dumps(self.manifest).encode() if name == 'host-release.json' else self.files[name]

    def install(self, **kwargs):
        with patch.object(updater, 'fetch', self.fetch), patch.object(updater, 'command', self.command), \
             patch.object(updater.time, 'sleep'), patch.object(Path, 'home', return_value=self.home), \
             patch.object(updater.subprocess, 'run', self.run), \
             patch.object(native_import_host, 'bind_managed_claude_repin', self.bind):
            updater.install(BASE, self.root, **kwargs)


@pytest.fixture
def world(tmp_path, monkeypatch):
    forbidden = lambda *a, **kw: pytest.fail('operator, global sync or provider refresh path used')
    for owner, name in ((config, 'load_config'), (bus, '_do_request'), (p, 'sync'), (p, 'tick'),
                        (native_import_host, 'bind'), (native_import_host, 'bind_managed_claude'),
                        (credentials, 'materialize_current'),
                        (p, 'install'), (p, 'report_sync'), (p, 'host_request'), (credentials, 'reconcile'),
                        (credentials, 'leased_refresh'), (credentials, 'fetch_current'),
                        (engine_login, 'tick'), (engine_login, 'cancel_removed')):
        monkeypatch.setattr(owner, name, forbidden)
    return World(tmp_path, monkeypatch)


def upgrade_world(world):
    old = world.make_release(OLD, 'old release')
    world.root.mkdir(exist_ok=True)
    (world.root / 'current').symlink_to(old)
    world.old_updater = b'# synthetic older updater without re-pin recovery\n'
    (world.root / 'updater.py').write_bytes(world.old_updater)
    world.configure_target(old)
    return old


def assert_rolled_back(world, old, original):
    assert (world.root / 'current').resolve() == old
    assert (world.root / 'updater.py').read_bytes() == world.old_updater
    assert not (world.root / updater.UPDATER_ROLLBACK).exists()
    assert not (world.releases / NEW).exists()
    assert NEW in json.loads((world.root / 'failed-releases.json').read_text())
    assert world.profile.read_text() == original
    assert world.bytes() == world.snapshot
    assert not (world.root / updater.REPIN_JOURNAL).exists()


def test_new_release_repins_same_generation_preserving_unrelated_bytes(world):
    old = upgrade_world(world)
    original = world.profile.read_text()
    world.install()
    new = world.releases / NEW
    assert (world.root / 'current').resolve() == new
    assert world.helper_runs == [NEW]
    assert world.helper_outputs == [{'status': 'repinned', 'generation': GENERATION, 'revision': REV,
                                     'previous_release': OLD, 'release': NEW}]
    assert world.pinned() == world.command_for(new)
    assert world.pinned()[:3] == [str(new / 'bin/python'), '-I', '-S']
    # Only the one generated command line changed; operator text, the other
    # managed profile, state, receipts and credential bytes are untouched.
    assert world.profile.read_text() == world.repinned_text(original, old, new)
    assert world.bytes() == world.snapshot
    observe = ('GET', f'/api/engines/hub-claude/generation?generation={GENERATION}&revision={REV}', None)
    ack = ('POST', '/api/engines/sync-report', {'state': 'synced', 'items': [{
        'label': 'hub-claude', 'revision': REV, 'state': 'installed', 'credential_generation': GENERATION}]})
    assert world.hub_calls == [observe, ack, observe, ack]
    assert (world.root / 'updater.py').read_bytes() == world.files['hub2-self-update.py']
    assert not (world.root / updater.UPDATER_ROLLBACK).exists()
    with as_release(new):
        assert p.managed_exec_readiness(new / 'hub.pyz')['profile'] == 'hub-hub-claude'
    assert not (world.root / updater.REPIN_JOURNAL).exists()
    assert SECRET not in (world.root / 'releases' / NEW / 'release.json').read_text()


def test_absent_profile_or_target_is_a_no_op_without_helper(world):
    old = world.make_release(OLD, 'old release')
    world.root.mkdir(exist_ok=True)
    (world.root / 'current').symlink_to(old)
    world.install()
    assert world.helper_runs == [] and world.hub_calls == []
    assert not world.managed.exists()
    # A profile without the managed target is also untouched and never parsed by Hub code.
    world.profile.parent.mkdir(mode=0o700)
    world.profile.write_text(OPERATOR_BEFORE)
    before = world.profile.read_bytes()
    world.install()  # same-version check path
    assert (world.root / 'current').resolve() == world.releases / NEW
    assert world.helper_runs == [] and world.profile.read_bytes() == before


@pytest.mark.parametrize('mutation', ['operator_owned', 'foreign_interpreter', 'unmanaged_root', 'duplicate_state'])
def test_malformed_or_foreign_target_refuses_and_rolls_back(world, mutation):
    old = upgrade_world(world)
    text = world.profile.read_text()
    old_line = 'command = ' + json.dumps(world.command_for(old))
    if mutation == 'operator_owned':
        block = text[text.index(p.BEGIN):text.index(p.END) + len(p.END)]
        managed = block.replace('[profiles.hub-hub-claude]\nengine = "claude"\n' + old_line + '\n', '')
        text = text.replace(block, managed) + '\n[profiles.hub-hub-claude]\nengine = "claude"\n' + old_line + '\n'
    elif mutation == 'foreign_interpreter':
        text = text.replace(old_line, 'command = ' + json.dumps(['/usr/bin/python3', '-c', 'pass', 'hub-claude']))
    elif mutation == 'unmanaged_root':
        foreign = world.command_for(old)
        text = text.replace(old_line, 'command = ' + json.dumps(
            [part.replace(str(world.managed), str(world.tmp / 'elsewhere')) for part in foreign]))
    else:
        rows = json.loads((world.managed / 'state.json').read_text())
        (world.managed / 'state.json').write_text(json.dumps(rows + [rows[1]]))
    world.profile.write_text(text)
    world.snapshot = world.bytes()
    with pytest.raises(RuntimeError, match='re-pin not verified'):
        world.install()
    assert_rolled_back(world, old, text)
    assert world.hub_calls == []


@pytest.mark.parametrize('change', ['newer_generation', 'older_generation', 'revision', 'rejected_or_leased', 'pre_ack'])
def test_hub_generation_revision_or_refusal_refuses_before_publication(world, change):
    old = upgrade_world(world)
    original = world.profile.read_text()
    if change == 'newer_generation':
        world.hub['generation'] = GENERATION + 1
    elif change == 'older_generation':
        world.hub['generation'] = GENERATION - 1
    elif change == 'revision':
        world.hub['revision'] = OTHER_REV
    elif change == 'rejected_or_leased':
        world.observation = RuntimeError('synthetic HTTP 409 refresh_in_progress')
    else:
        world.acks = [{'status': 'held'}]
    with pytest.raises(RuntimeError, match='re-pin not verified'):
        world.install()
    assert_rolled_back(world, old, original)
    assert [method for method, _, _ in world.hub_calls] == (['GET', 'POST'] if change == 'pre_ack' else ['GET'])
    assert (world.root / 'updater.py').read_bytes() == world.old_updater


def test_local_generation_state_mismatch_refuses(world):
    old = upgrade_world(world)
    original = world.profile.read_text()
    (world.managed / ('hub-claude-' + REV) / 'home' / PATH).write_text('{"claudeAiOauth": {}}')
    world.snapshot = world.bytes()
    with pytest.raises(RuntimeError):
        world.install()
    assert_rolled_back(world, old, original)
    assert world.hub_calls == []


@pytest.mark.parametrize('expiry', [None, 'unknown', 1000, -1])
def test_digest_verified_local_expiry_missing_unknown_or_expired_refuses(world, expiry):
    old = upgrade_world(world)
    original = world.profile.read_text()
    oauth = {'accessToken': SECRET, 'refreshToken': ''}
    if expiry is not None:
        oauth['expiresAt'] = int(time.time() * 1000) - 1000 if expiry == -1 else expiry
    data = json.dumps({'claudeAiOauth': oauth})
    dest = world.managed / ('hub-claude-' + REV)
    (dest / 'home' / PATH).write_text(data)
    p.write_json(dest / credentials.STATE_FILE, {'generation': GENERATION, 'digests': {PATH: hashlib.sha256(data.encode()).hexdigest()}})
    world.snapshot = world.bytes()
    with pytest.raises(RuntimeError):
        world.install()
    assert_rolled_back(world, old, original)
    assert world.hub_calls == []


@pytest.mark.parametrize('ack', [{'status': 'held'}, {}, None, RuntimeError('synthetic transport failure')])
def test_unrecorded_post_ack_restores_published_pin_updater_and_release(world, ack):
    old = upgrade_world(world)
    original = world.profile.read_text()
    world.acks = [{'status': 'recorded'}, ack]
    published = []
    world.on_ack = lambda index: index == 1 and published.append(
        world.pinned() == world.command_for(world.releases / NEW) != world.command_for(old))
    with pytest.raises(RuntimeError, match='re-pin not verified'):
        world.install()
    assert published == [True]  # the new release pin was published before the failed ACK
    assert_rolled_back(world, old, original)
    assert (world.root / 'updater.py').read_bytes() == world.old_updater
    assert not (world.root / updater.UPDATER_ROLLBACK).exists()


def test_failed_readiness_restores_published_pin(world, monkeypatch):
    old = upgrade_world(world)
    original = world.profile.read_text()
    def unavailable(archive):
        raise ValueError('managed_exec_unavailable')
    monkeypatch.setattr(p, 'managed_exec_readiness', unavailable)
    with pytest.raises(RuntimeError):
        world.install()
    assert_rolled_back(world, old, original)
    assert sum(path.endswith('sync-report') for _, path, _ in world.hub_calls) == 1  # pre-publication only


def test_helper_timeout_after_publication_restores_from_journal(world):
    old = upgrade_world(world)
    original = world.profile.read_text()
    def killed():
        assert world.pinned() == world.command_for(world.releases / NEW)
        raise subprocess.TimeoutExpired('helper', 120)
    world.helper_hook = killed
    with pytest.raises(RuntimeError):
        world.install()
    assert_rolled_back(world, old, original)


def test_rollback_after_publication_keeps_concurrent_unrelated_changes(world):
    old = upgrade_world(world)
    world.acks = [{'status': 'recorded'}, {'status': 'held'}]
    def concurrent(index):
        if index != 1:
            return
        # Another writer changes operator text and an unrelated managed table.
        text = world.profile.read_text().replace('engine = "shell"\ncommand = ["sh"]', 'engine = "shell"\ncommand = ["bash"]')
        world.profile.write_text(text + '\n[profiles.late]\nengine = "shell"\n')
        (world.managed / ('test-account-' + OTHER_REV) / 'keep.bin').write_bytes(b'concurrent unrelated bytes')
    world.on_ack = concurrent
    with pytest.raises(RuntimeError):
        world.install()
    text = world.profile.read_text()
    assert world.pinned() == world.command_for(old)
    assert 'command = ["bash"]' in text and '[profiles.late]' in text
    assert (world.managed / ('test-account-' + OTHER_REV) / 'keep.bin').read_bytes() == b'concurrent unrelated bytes'


def recover_world(world):
    with patch.object(updater, 'command', world.command), patch.object(Path, 'home', return_value=world.home):
        updater.recover(world.root, world.releases)


@pytest.mark.parametrize('owner', ['newer_generation', 'newer_revision', 'newer_args_same_release'])
def test_owner_change_still_naming_candidate_keeps_candidate_and_journal(world, owner):
    """A G/R or command change is not proof the candidate is unreferenced."""
    old = upgrade_world(world)
    world.acks = [{'status': 'recorded'}, {'status': 'held'}]
    candidate = world.releases / NEW
    def takeover(index):
        if index != 1:
            return
        dest = world.managed / ('hub-claude-' + REV)
        if owner == 'newer_generation':
            state = json.loads((dest / credentials.STATE_FILE).read_text())
            p.write_json(dest / credentials.STATE_FILE, dict(state, generation=GENERATION + 1))
        elif owner == 'newer_revision':
            rows = json.loads((world.managed / 'state.json').read_text())
            rows[1]['revision'] = OTHER_REV.replace('1', '4')
            (world.managed / 'state.json').write_text(json.dumps(rows))
        else:
            line = 'command = ' + json.dumps(world.command_for(candidate))
            newer = [str(candidate / 'bin/python'), '-I', '-c', 'pass', 'hub-claude']
            world.profile.write_text(world.profile.read_text().replace(line, 'command = ' + json.dumps(newer)))
        world.after_takeover = world.profile.read_bytes()
    world.on_ack = takeover
    with pytest.raises(RuntimeError, match='pin restore pending'):
        world.install()
    # Newer bytes preserved, and the pin they hold still resolves.
    assert world.profile.read_bytes() == world.after_takeover
    assert str(candidate) in world.profile.read_text()
    assert candidate.is_dir() and (candidate / 'bin/python').is_file()
    assert (world.root / updater.REPIN_JOURNAL).exists()
    assert (world.root / 'updater.py').read_bytes() == world.files['hub2-self-update.py']
    assert (world.root / 'current').resolve() == old
    # Recovery fails visibly while the candidate is still named.
    with pytest.raises(RuntimeError, match='pin restore pending'):
        recover_world(world)
    assert candidate.is_dir() and (world.root / updater.REPIN_JOURNAL).exists()
    assert world.profile.read_bytes() == world.after_takeover
    # The newer owner moves off the candidate: positive unreference proof.
    newest = world.profile.read_text().replace(str(candidate), str(old))
    world.profile.write_text(newest)
    recover_world(world)
    assert world.profile.read_text() == newest
    assert not candidate.exists() and not (world.root / updater.REPIN_JOURNAL).exists()
    assert (world.root / 'updater.py').read_bytes() == world.old_updater


def test_newer_owner_off_candidate_is_superseded_and_preserved(world):
    old = upgrade_world(world)
    world.acks = [{'status': 'recorded'}, {'status': 'held'}]
    newer = ['/synthetic/newer/owner/python', '-c', 'pass', 'hub-claude']
    def takeover(index):
        if index != 1:
            return
        line = 'command = ' + json.dumps(world.command_for(world.releases / NEW))
        world.profile.write_text(world.profile.read_text().replace(line, 'command = ' + json.dumps(newer)))
        world.after_takeover = world.profile.read_bytes()
    world.on_ack = takeover
    with pytest.raises(RuntimeError, match='re-pin not verified'):
        world.install()
    assert world.profile.read_bytes() == world.after_takeover
    assert not (world.root / updater.REPIN_JOURNAL).exists()
    assert not (world.releases / NEW).exists()
    assert (world.root / 'current').resolve() == old


class Barriers:
    """Records each directory fsync with the recovery metadata visible at that moment."""

    def __init__(self, world, fail=None, once=True):
        self.world, self.fail, self.once, self.events, self.real = world, fail, once, [], updater.sync_directory

    def __call__(self, directory):
        root = self.world.root
        event = {'path': Path(directory), 'journal': (root / updater.REPIN_JOURNAL).exists(),
                 'marker': (self.world.releases / NEW / '.installing').exists(),
                 'updater_record': (root / updater.UPDATER_ROLLBACK).exists(),
                 'pin': self.world.pinned() if self.world.profile.exists() else None}
        self.events.append(event)
        if self.fail and self.fail(event):
            if self.once:
                self.fail = None
            raise OSError('synthetic crash at durability barrier')
        self.real(directory)


def test_restore_fsyncs_profile_directory_before_journal_retirement(world):
    old = upgrade_world(world)
    original = world.profile.read_text()
    world.acks = [{'status': 'recorded'}, {'status': 'held'}]
    barriers = Barriers(world)
    with patch.object(updater, 'sync_directory', barriers), pytest.raises(RuntimeError, match='re-pin not verified'):
        world.install()
    assert_rolled_back(world, old, original)
    restored = [e for e in barriers.events if e['path'] == world.profile.parent and e['pin'] == world.command_for(old)]
    assert restored and all(e['journal'] for e in restored)


def test_failed_profile_directory_fsync_keeps_journal_and_retry_repairs_it(world):
    old = upgrade_world(world)
    original = world.profile.read_text()
    world.acks = [{'status': 'recorded'}, {'status': 'held'}]
    # Power-loss window: the restored bytes were replaced, the directory entry is
    # not durable, for every restore attempt the failed install makes.
    barriers = Barriers(world, fail=lambda e: e['path'] == world.profile.parent and e['pin'] == world.command_for(old),
                        once=False)
    with patch.object(updater, 'sync_directory', barriers), pytest.raises(RuntimeError, match='pin restore pending'):
        world.install()
    assert (world.root / updater.REPIN_JOURNAL).exists() and (world.releases / NEW).is_dir()
    assert world.pinned() == world.command_for(old)  # replaced, but not yet proven durable
    retry = Barriers(world)
    with patch.object(updater, 'sync_directory', retry):
        recover_world(world)
    assert_rolled_back(world, old, original)
    # The retry found the previous pin already in place and still made it durable
    # before retiring the journal.
    repaired = [e for e in retry.events if e['path'] == world.profile.parent]
    assert repaired and all(e['journal'] for e in repaired)


def test_activation_barriers_precede_retirement_of_recovery_metadata(world):
    upgrade_world(world)
    barriers = Barriers(world)
    with patch.object(updater, 'sync_directory', barriers):
        world.install()
    candidate = world.releases / NEW
    def retired(key):
        written = next(i for i, e in enumerate(barriers.events) if e[key])
        return next(i for i, e in enumerate(barriers.events) if i > written and not e[key])
    before = barriers.events[:retired('journal')]
    for path in (candidate, world.releases, world.root, world.profile.parent):
        assert any(e['path'] == path and not e['marker'] for e in before), path
    assert not (world.root / updater.REPIN_JOURNAL).exists() and not (world.root / updater.UPDATER_ROLLBACK).exists()
    assert any(e['path'] == candidate and not e['marker'] for e in barriers.events[:retired('updater_record')])


@pytest.mark.parametrize('resurrected', [False, True])
def test_crash_at_activation_barrier_keeps_journal_and_recovers_consistently(world, resurrected):
    old = upgrade_world(world)
    original = world.profile.read_text()
    candidate = world.releases / NEW
    barriers = Barriers(world, fail=lambda e: e['path'] == candidate and not e['marker'] and e['journal'])
    with patch.object(updater, 'sync_directory', barriers), pytest.raises(OSError, match='durability barrier'):
        world.install()
    # The journal was not retired: the marker removal was never proven durable.
    state = json.loads((world.root / updater.REPIN_JOURNAL).read_text())
    assert state['verified'] is True and world.pinned() == world.command_for(candidate)
    if resurrected:
        # Power loss lost the marker unlink: recovery sees an interrupted release.
        (candidate / '.installing').write_text(NEW)
    recover_world(world)
    assert not (world.root / updater.REPIN_JOURNAL).exists()
    if resurrected:
        assert (world.root / 'current').resolve() == old and not candidate.exists()
        assert world.profile.read_text() == original
    else:
        assert (world.root / 'current').resolve() == candidate
        assert world.pinned() == world.command_for(candidate)
    assert Path(world.pinned()[0]).is_file()  # never a dangling pin
    assert world.bytes() == world.snapshot


@pytest.mark.parametrize('stage', ['marker', 'retained_switch', 'unverified'])
def test_commit_refuses_to_retire_journal_before_activation_commits(world, stage):
    upgrade_world(world)
    world.install()
    candidate = world.releases / NEW
    journal = {'schema': 1, 'release': NEW, 'previous_release': OLD, 'profile_path': str(world.profile),
               'managed_root': str(world.managed), 'previous_command': world.command_for(world.releases / OLD),
               'command': world.command_for(candidate), 'generation': GENERATION, 'revision': REV,
               'verified': stage != 'unverified'}
    (world.root / updater.REPIN_JOURNAL).write_text(json.dumps(journal))
    if stage == 'marker':
        (candidate / '.installing').write_text(NEW)
    elif stage == 'retained_switch':
        (world.root / 'retained-switch.json').write_text('{}')
    with pytest.raises(ValueError):
        updater.commit_repin(world.root, world.releases)
    assert (world.root / updater.REPIN_JOURNAL).exists() and candidate.is_dir()


def test_held_managed_lock_is_bounded_and_refuses_without_changes(world, monkeypatch):
    import fcntl
    old = upgrade_world(world)
    original = world.profile.read_text()
    monkeypatch.setattr(repin, 'LOCK_TIMEOUT_S', 0.2)
    fd = os.open(world.managed / 'reconcile.lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(RuntimeError):
            world.install()
    finally:
        os.close(fd)
    assert_rolled_back(world, old, original)
    assert world.hub_calls == []


def test_same_version_heals_stale_pin_then_is_current_without_network(world):
    old = world.make_release(OLD, 'old release')
    world.root.mkdir(exist_ok=True)
    world.install(restart=False)  # first activation; no target configured yet
    new = world.releases / NEW
    world.configure_target(old)  # the observed stale pin after an earlier activation
    original = world.profile.read_text()
    world.install()
    assert world.profile.read_text() == world.repinned_text(original, old, new)
    assert world.bytes() == world.snapshot
    assert [method for method, _, _ in world.hub_calls] == ['GET', 'POST', 'GET', 'POST']
    world.hub_calls.clear()
    world.install()
    assert world.hub_calls == [] and world.helper_runs == [NEW, NEW]
    assert not (world.root / updater.REPIN_JOURNAL).exists()


def test_same_version_generation_conflict_fails_closed_without_changes(world):
    old = world.make_release(OLD, 'old release')
    world.root.mkdir(exist_ok=True)
    world.install(restart=False)
    world.configure_target(old)
    original = world.profile.read_text()
    world.hub['generation'] = GENERATION + 1
    with pytest.raises(RuntimeError):
        world.install()
    assert world.profile.read_text() == original and world.bytes() == world.snapshot
    assert (world.root / 'current').resolve() == world.releases / NEW


def prepare_retained(world):
    old = world.make_release(OLD, 'old release')
    world.root.mkdir(exist_ok=True)
    retained = world.releases / NEW
    (retained / 'bin').mkdir(parents=True)
    (retained / 'bin/python').write_text('synthetic interpreter, never executed\n')
    (retained / 'hub.pyz').write_bytes(world.files['hub.pyz'])
    (retained / 'release.json').write_text(json.dumps(world.manifest))
    (world.root / 'current').symlink_to(old)
    world.configure_target(old)
    return old


def test_retained_release_reactivation_repins(world):
    old = prepare_retained(world)
    original = world.profile.read_text()
    world.install()
    new = world.releases / NEW
    assert (world.root / 'current').resolve() == new
    assert world.profile.read_text() == world.repinned_text(original, old, new)
    assert world.bytes() == world.snapshot
    assert not (world.root / updater.REPIN_JOURNAL).exists()
    assert not (world.releases / ('.retained-' + NEW)).exists()


def test_retained_failure_after_publication_restores_pin_and_switch(world):
    old = prepare_retained(world)
    original = world.profile.read_text()
    link = updater.link
    def fail_previous(target, path):
        if path.name == 'previous':
            raise OSError('synthetic interrupted retained commit')
        return link(target, path)
    with patch.object(updater, 'link', fail_previous):
        with pytest.raises(OSError, match='retained commit'):
            world.install()
    assert world.helper_runs == [NEW]
    assert (world.root / 'current').resolve() == old
    assert world.profile.read_text() == original and world.bytes() == world.snapshot
    assert not (world.root / updater.REPIN_JOURNAL).exists()
    assert not (world.root / 'retained-switch.json').exists()


@pytest.mark.parametrize('verified', [False, True])
def test_recovery_restores_unverified_and_commits_verified_pin(world, verified):
    old = upgrade_world(world)
    original = world.profile.read_text()
    world.install()
    new = world.releases / NEW
    repinned = world.profile.read_text()
    journal = {'schema': 1, 'release': NEW, 'previous_release': OLD, 'profile_path': str(world.profile),
               'managed_root': str(world.managed), 'previous_command': world.command_for(old),
               'command': world.command_for(new), 'generation': GENERATION, 'revision': REV, 'verified': verified}
    (world.root / updater.REPIN_JOURNAL).write_text(json.dumps(journal))
    updater.recover(world.root, world.releases)
    assert world.profile.read_text() == (repinned if verified else original)
    assert not (world.root / updater.REPIN_JOURNAL).exists()


def test_helper_refuses_explicit_or_legacy_authority(world, monkeypatch):
    old = upgrade_world(world)
    original = world.profile.read_text()
    world.enrollment.unlink()  # legacy host.json fallback is never used
    with pytest.raises(RuntimeError):
        world.install(restart=False)
    assert_rolled_back(world, old, original)
    assert world.hub_calls == []


def crash_snapshot(world):
    """On-disk state exactly as a SIGKILL at this point would leave it."""
    saved = world.tmp / 'crash'
    shutil.copytree(world.root, saved / 'root', symlinks=True)
    shutil.copytree(world.managed, saved / 'managed', symlinks=True)
    shutil.copy2(world.profile, saved / 'config.toml')
    return saved


def restore_snapshot(world, saved):
    shutil.rmtree(world.root)
    shutil.rmtree(world.managed)
    shutil.copytree(saved / 'root', world.root, symlinks=True)
    shutil.copytree(saved / 'managed', world.managed, symlinks=True)
    shutil.copy2(saved / 'config.toml', world.profile)


def test_sigkill_after_publication_is_recovered_by_the_advanced_updater(world):
    old = upgrade_world(world)
    original = world.profile.read_text()
    snapshots = []
    world.acks = [{'status': 'recorded'}, {'status': 'held'}]
    world.on_ack = lambda index: index == 1 and snapshots.append(crash_snapshot(world))
    with pytest.raises(RuntimeError):
        world.install()
    restore_snapshot(world, snapshots[0])
    candidate = world.releases / NEW
    # The killed state: candidate current/marked, pin published, journal pending,
    # and the on-disk updater is the journal-aware one, never the old updater.
    assert (candidate / '.installing').exists() and (world.root / updater.REPIN_JOURNAL).exists()
    assert world.pinned() == world.command_for(candidate)
    assert (world.root / 'updater.py').read_bytes() == world.files['hub2-self-update.py']
    with patch.object(updater, 'command', world.command), patch.object(Path, 'home', return_value=world.home):
        updater.recover(world.root, world.releases)
    assert_rolled_back(world, old, original)


def test_failed_restore_keeps_candidate_journal_and_fails_visibly_until_recovered(world, monkeypatch):
    import fcntl
    old = upgrade_world(world)
    original = world.profile.read_text()
    monkeypatch.setattr(updater, 'REPIN_LOCK_TIMEOUT', 0.2)
    holder = []
    def hold(index):
        if index == 1:
            fd = os.open(world.managed / 'reconcile.lock', os.O_RDWR)
            holder.append(fd)
    world.acks = [{'status': 'recorded'}, {'status': 'held'}]
    world.on_ack = hold
    # The helper's own lock is released when it returns; the competing holder
    # acquires afterwards, before the updater's restore attempt.
    world.helper_hook = lambda: holder and fcntl.flock(holder[0], fcntl.LOCK_EX)
    try:
        with pytest.raises(RuntimeError, match='pin restore pending'):
            world.install()
        candidate = world.releases / NEW
        assert candidate.is_dir() and (candidate / '.installing').exists()
        assert (world.root / updater.REPIN_JOURNAL).exists()
        assert world.pinned() == world.command_for(candidate)
        assert (world.root / 'current').resolve() == old
        assert (world.root / 'updater.py').read_bytes() == world.files['hub2-self-update.py']
        with patch.object(updater, 'command', world.command), patch.object(Path, 'home', return_value=world.home):
            with pytest.raises(TimeoutError):
                updater.recover(world.root, world.releases)
        assert candidate.is_dir() and (world.root / updater.REPIN_JOURNAL).exists()
    finally:
        for fd in holder:
            os.close(fd)
    with patch.object(updater, 'command', world.command), patch.object(Path, 'home', return_value=world.home):
        updater.recover(world.root, world.releases)
    assert_rolled_back(world, old, original)


def test_retained_failed_restore_keeps_switch_journal_and_backup(world, monkeypatch):
    import fcntl
    old = prepare_retained(world)
    world.old_updater = b'# synthetic older updater\n'
    (world.root / 'updater.py').write_bytes(world.old_updater)
    original = world.profile.read_text()
    monkeypatch.setattr(updater, 'REPIN_LOCK_TIMEOUT', 0.2)
    holder = []
    world.acks = [{'status': 'recorded'}, {'status': 'held'}]
    world.on_ack = lambda index: index == 1 and holder.append(os.open(world.managed / 'reconcile.lock', os.O_RDWR))
    world.helper_hook = lambda: holder and fcntl.flock(holder[0], fcntl.LOCK_EX)
    try:
        with pytest.raises(RuntimeError, match='pin restore pending'):
            world.install()
        assert (world.root / 'retained-switch.json').exists()
        assert (world.releases / ('.retained-' + NEW)).is_dir()
        assert (world.root / updater.REPIN_JOURNAL).exists()
    finally:
        for fd in holder:
            os.close(fd)
    with patch.object(updater, 'command', world.command), patch.object(Path, 'home', return_value=world.home):
        updater.recover(world.root, world.releases)
    assert (world.root / 'current').resolve() == old
    assert world.profile.read_text() == original and world.bytes() == world.snapshot
    assert not (world.root / 'retained-switch.json').exists()
    assert (world.root / 'updater.py').read_bytes() == world.old_updater


def test_repin_binding_is_current_enrollment_only_with_two_exact_routes(tmp_path, monkeypatch):
    from agent_hub_host import api
    home = tmp_path / 'binding-home'
    profiles = home / '.config/agent-hub'
    profiles.mkdir(parents=True, mode=0o700)
    home.chmod(0o700); (home / '.config').chmod(0o700)
    for name in ('HUB_CONFIG_FILE', 'HUB_TOKEN', 'HUB_JOB_TOKEN', 'HUB_WORKER_CONFIG_FILE', 'HUB2_CONFIG', 'APLEXER_TAG'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
    v2 = profiles / 'v2.json'
    v2.write_text(json.dumps({'server': 'https://hub.test', 'token': 'synthetic-host-token', 'machine_id': 'test-host'}))
    v2.chmod(0o600)
    sent = []
    monkeypatch.setattr(api, 'safe_http_json', lambda request: sent.append(request) or {'status': 'recorded'})
    machine, send = native_import_host.bind_managed_claude_repin('https://hub.test')
    assert machine == 'test-host'
    check = f'/api/engines/hub-claude/generation?generation=7&revision={REV}'
    ack = {'state': 'synced', 'items': [{'label': 'hub-claude', 'revision': REV, 'state': 'installed',
                                          'credential_generation': 7}]}
    send('GET', check)
    send('POST', '/api/engines/sync-report', ack)
    refused = [('GET', '/api/engines/sync', None), ('GET', '/api/engines/hub-claude/credential', None),
               ('POST', '/api/engines/hub-claude/credential', {}), ('POST', '/api/engines/admission',
               {'label': 'hub-claude', 'native': True}), ('POST', '/api/engines/hub-claude', {}),
               ('GET', check + '&min_validity_ms=10800000', None), ('GET', check.replace('=7&', '=07&'), None),
               ('GET', check.replace('generation=7', 'generation=7&generation=8'), None),
               ('GET', '/api/engines/hub-claude/generation?revision=' + REV + '&generation=7', None),
               ('POST', check, None), ('POST', '/api/engines/sync-report', {'state': 'synced', 'items': []}),
               ('POST', '/api/engines/sync-report', {'state': 'held', 'items': ack['items']})]
    for method, path, body in refused:
        with pytest.raises(ValueError):
            send(method, path, body)
    assert len(sent) == 2
    assert all(request.full_url.startswith('https://hub.test/api/engines/') for request in sent)
    monkeypatch.setenv('HUB_CONFIG_FILE', str(v2))
    with pytest.raises(ValueError):
        native_import_host.bind_managed_claude_repin('https://hub.test')
    monkeypatch.delenv('HUB_CONFIG_FILE')
    v2.unlink()
    (profiles / 'host.json').write_text(json.dumps({'base_url': 'https://hub.test', 'access_token': 'synthetic-legacy',
                                                   'machine_id': 'legacy-host'}))
    (profiles / 'host.json').chmod(0o600)
    with pytest.raises(ValueError):
        native_import_host.bind_managed_claude_repin('https://hub.test')
    assert len(sent) == 2
