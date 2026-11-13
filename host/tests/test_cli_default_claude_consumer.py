"""Opt-in ONE-WAY default Claude consumer and its native store/lock coordination.

Synthetic only: a temp HOME/native store, a temp managed root, synthetic token
markers, separate owned processes for the native-lock contenders. No real
credential, config, enrollment, network, provider or service is touched; any
socket use fails the test.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import textwrap
import time

import pytest

from agent_hub_host import (default_claude_consumer as dcc, managed_claude_deliver as deliver,
                            native_claude_store as native, provisioning)

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='default Claude consumer is Linux only')

REPO = Path(__file__).resolve().parents[2]
REV = '33333333-3333-4333-8333-333333333333'
PATH = '.claude/.credentials.json'
EXPIRES = int(time.time() * 1000) + 86400000
SECRETS = ('syn-access-', 'syn-refresh-', 'syn-mcp-', 'syn-native-')


def oauth(generation, refresh=''):
    return {'accessToken': 'syn-access-%d' % generation, 'refreshToken': refresh, 'expiresAt': EXPIRES + generation,
            'scopes': ['user:inference']}


def view(generation, revision=REV, body=None):
    body = oauth(generation) if body is None else body
    return {'label': 'hub-claude', 'generation': generation, 'revision': revision, 'status': 'current',
            'files': {PATH: json.dumps({'claudeAiOauth': body})}}


def request(generation, revision=REV, body=None):
    value = view(generation, revision, body)
    return {key: value[key] for key in ('label', 'generation', 'revision', 'files')}


MCP = {'mcpOAuth': {'srv|1': {'accessToken': 'syn-mcp-token', 'expiresAt': 5}}, 'other': [1, 2]}


class World:
    def __init__(self, tmp_path, monkeypatch):
        self.tmp = tmp_path
        self.home = tmp_path / 'home'
        self.home.mkdir(mode=0o700)
        self.store = self.home / '.claude'
        self.store.mkdir(mode=0o700)
        self.managed = tmp_path / 'managed'
        self.env = {'HOME': str(self.home), 'AGENT_HUB_MANAGED_DIR': str(self.managed)}
        for key, value in self.env.items():
            monkeypatch.setenv(key, value)
        for key in ('CLAUDE_CONFIG_DIR', 'CLAUDE_SECURESTORAGE_CONFIG_DIR',
                    'HUB2_CONFIG', 'APLEXER_TAG', 'APLEXER_WORKSPACE', 'APLEXER_SESSION_ID'):
            monkeypatch.delenv(key, raising=False)
        self.bin = tmp_path / 'bin'
        self.bin.mkdir(mode=0o755)
        self.claude = self.bin / 'claude'
        self.set_version('2.1.322')
        monkeypatch.setenv('PATH', str(self.bin) + os.pathsep + os.environ.get('PATH', ''))
        monkeypatch.setattr(dcc, 'NATIVE_WAIT_S', 0.6)
        monkeypatch.setattr(dcc, 'FAULT', None)

        def refuse(*args, **kwargs):
            raise AssertionError('network use is forbidden')
        monkeypatch.setattr(socket.socket, 'connect', refuse)
        self.cred = self.store / '.credentials.json'

    def set_version(self, text, code=0):
        # A synthetic stand-in for the public executable: never the real Claude, no auth.
        # A fresh file (new inode) models a package upgrade replacing the executable.
        staging = self.bin / 'claude.new'
        staging.write_text('#!/bin/sh\necho %s\nexit %d\n' % (text, code))
        os.chmod(staging, 0o755)
        os.replace(staging, self.claude)

    def put(self, body, extra=None, raw=None):
        document = {'claudeAiOauth': body}
        document.update(MCP if extra is None else extra)
        self.cred.write_bytes(raw if raw is not None else json.dumps(document, indent=2).encode())
        os.chmod(self.cred, 0o600)

    def doc(self):
        return json.loads(self.cred.read_bytes())

    def raw(self):
        return self.cred.read_bytes()

    def enroll(self, generation=7, **kw):
        return dcc.enroll(lambda: view(generation), confirm_same_account=True, **kw)

    def state(self):
        return json.loads((self.managed / dcc.STATE_NAME).read_text())

    def write_mode(self):
        dcc.set_mode('write', confirm_writer_handoff=True)


@pytest.fixture
def world(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


@pytest.fixture
def enrolled(world):
    world.put(oauth(7))
    world.enroll(7)
    return world


@pytest.fixture
def writing(enrolled):
    enrolled.write_mode()
    return enrolled


def no_secret(*texts):
    blob = ' '.join(str(t) for t in texts)
    assert not any(marker in blob for marker in SECRETS), blob


# ------------------------------------------------------------ enrollment

def test_same_generation_enrollment_binds_exact_store_and_starts_observe_only(world):
    world.put(oauth(7))
    before = world.raw()
    result = world.enroll(7)
    state = world.state()
    assert result['status'] == 'enrolled' and result['mode'] == 'observe'
    assert state['mode'] == 'observe' and state['protocol'] == native.PROTOCOL
    assert state['store'] == str(world.store) and state['revision'] == REV and state['label'] == 'hub-claude'
    assert state['committed']['generation'] == 7 and state['pending'] is None
    assert state['account'] == 'claude' and len(state['consumer_id']) == 32
    assert world.raw() == before
    no_secret(json.dumps(result), json.dumps(state))
    assert oct((world.managed / dcc.STATE_NAME).stat().st_mode & 0o777) == '0o600'


def test_enrollment_requires_oauth_subdocument_equality_not_expiry_or_whole_document(world):
    # Unrelated entries differ from Hub's document: still equal OAuth subdocument.
    world.put(oauth(7), extra={'mcpOAuth': {'x': 1}})
    assert world.enroll(7)['status'] == 'enrolled'
    dcc.disable()
    # A later expiry or a rotated refresh token is NOT the approved payload.
    for body in (dict(oauth(7), expiresAt=EXPIRES + 99), dict(oauth(7), refreshToken='syn-refresh-x'),
                 dict(oauth(7), accessToken='syn-access-other')):
        world.put(body)
        with pytest.raises(dcc.Refused, match='local_differs_from_central'):
            world.enroll(7)
        assert not (world.managed / dcc.STATE_NAME).exists()


def test_enrollment_refuses_without_confirmation_and_for_unusable_central_payloads(world):
    world.put(oauth(7))
    with pytest.raises(dcc.Refused, match='confirmation_required'):
        dcc.enroll(lambda: view(7), confirm_same_account=False)
    bad = [dict(view(7), label='other'), dict(view(7), status='stale'), dict(view(7), generation=0),
           dict(view(7), revision='not-a-revision'), dict(view(7), files={}),
           dict(view(7), files={PATH: '{}'}), dict(view(7), files={PATH: 'not json'})]
    for item in bad:
        with pytest.raises(dcc.Refused):
            world.enroll_with = dcc.enroll(lambda item=item: item, confirm_same_account=True)
    assert not (world.managed / dcc.STATE_NAME).exists()


@pytest.mark.parametrize('case', ['absent', 'malformed', 'no_oauth', 'duplicate_keys', 'symlink', 'hardlink',
                                  'store_symlink', 'group_writable_store', 'oversize'])
def test_enrollment_refuses_unsafe_or_foreign_stores_without_mutation(world, case):
    world.put(oauth(7))
    good = world.raw()
    if case == 'absent':
        world.cred.unlink()
    elif case == 'malformed':
        world.cred.write_bytes(b'{not json')
    elif case == 'no_oauth':
        world.put(oauth(7), extra={})
        world.cred.write_text('{"mcpOAuth": {}}')
    elif case == 'duplicate_keys':
        world.cred.write_text('{"claudeAiOauth": {"accessToken":"a"}, "claudeAiOauth": {"accessToken":"b"}}')
    elif case == 'symlink':
        real = world.tmp / 'real.json'
        real.write_bytes(good)
        world.cred.unlink()
        world.cred.symlink_to(real)
    elif case == 'hardlink':
        os.link(world.cred, world.store / 'alias')
    elif case == 'store_symlink':
        real = world.tmp / 'realstore'
        world.store.rename(real)
        world.store.symlink_to(real)
    elif case == 'group_writable_store':
        os.chmod(world.store, 0o775)
    elif case == 'oversize':
        world.cred.write_bytes(good + b' ' * (native.MAX_DOCUMENT + 1))
    with pytest.raises(dcc.Refused):
        world.enroll(7)
    assert not (world.managed / dcc.STATE_NAME).exists()


def test_effective_store_honours_securestorage_env_before_ordinary_config(world, tmp_path):
    other = tmp_path / 'other-store'
    other.mkdir(mode=0o700)
    env = {'CLAUDE_SECURESTORAGE_CONFIG_DIR': str(other), 'CLAUDE_CONFIG_DIR': str(world.store)}
    assert native.effective_store(env, world.home) == other
    assert native.effective_store({'CLAUDE_CONFIG_DIR': str(other)}, world.home) == other
    assert native.effective_store({'CLAUDE_SECURESTORAGE_CONFIG_DIR': ''}, world.home) == world.store
    assert native.effective_store({}, world.home) == world.store
    with pytest.raises(native.StoreRefused):
        native.effective_store({'CLAUDE_SECURESTORAGE_CONFIG_DIR': 'relative/dir'}, world.home)
    # Enrollment pins that exact (env-selected) store.
    (other / '.credentials.json').write_text(json.dumps({'claudeAiOauth': oauth(7)}))
    world.put(oauth(1))  # the ordinary store is NOT the one that gets bound
    dcc.enroll(lambda: view(7), confirm_same_account=True, environ=env)
    assert world.state()['store'] == str(other)


def test_re_enrollment_only_for_same_store_and_known_content(enrolled, tmp_path):
    first = enrolled.state()
    enrolled.put(oauth(8))
    again = dcc.enroll(lambda: view(8, revision='44444444-4444-4444-8444-444444444444'), confirm_same_account=True)
    assert again['consumer_id'] == first['consumer_id']
    assert enrolled.state()['revision'] == '44444444-4444-4444-8444-444444444444'
    enrolled.put(oauth(5))
    with pytest.raises(dcc.Refused, match='generation_refused'):
        enrolled.enroll(5)
    other = tmp_path / 'elsewhere'
    other.mkdir(mode=0o700)
    (other / '.credentials.json').write_text(json.dumps({'claudeAiOauth': oauth(9)}))
    with pytest.raises(dcc.Refused, match='enrolled_elsewhere'):
        dcc.enroll(lambda: view(9), confirm_same_account=True,
                   environ={'CLAUDE_SECURESTORAGE_CONFIG_DIR': str(other)})


def test_commands_live_under_the_existing_engines_parser_and_writes_need_handoff_confirmation(enrolled):
    parser = argparse.ArgumentParser()
    provisioning.add_commands(parser.add_subparsers(dest='cmd'))
    for words in (['enroll', '--confirm-same-account'], ['status'], ['check'], ['disable'], ['observe'],
                  ['enable-writes', '--confirm-writer-handoff']):
        args = parser.parse_args(['engines', 'default-claude'] + words)
        assert args.engines_cmd == 'default-claude' and args.default_claude_cmd == words[0]
    with pytest.raises(dcc.Refused, match='writer_handoff_confirmation_required'):
        dcc.set_mode('write')
    assert enrolled.state()['mode'] == 'observe'
    args = parser.parse_args(['engines', 'default-claude', 'status'])
    result = provisioning.command(args)
    assert result['enrolled'] and result['local'] == 'known' and result['mode'] == 'observe'
    no_secret(json.dumps(result))
    assert dcc.disable() == {'enrolled': False, 'removed': True}
    assert provisioning.command(args) == {'enrolled': False}


# ------------------------------------------------------- absence / observe

def test_absence_is_a_noop_and_managed_only_behavior_is_unchanged(world):
    world.put(oauth(1))
    before = world.raw()
    assert dcc.deliver(request(8)) == {'status': 'absent'}
    assert world.raw() == before
    assert not (world.managed / dcc.STATE_NAME).exists()
    # The helper without enrollment and without any managed state is the old 'absent'.
    assert deliver.deliver('/nonexistent/hub.pyz', request(8)) == {'status': 'absent'}
    assert dcc.current_set(False) == dcc.consumer_set(False, None)


def test_observe_mode_never_writes_or_claims_delivery(enrolled):
    before, state = enrolled.raw(), (enrolled.managed / dcc.STATE_NAME).read_bytes()
    assert dcc.deliver(request(8)) == {'status': 'observe_only', 'enabled': False}
    assert dcc.deliver(request(7)) == {'status': 'observe_only', 'enabled': False}
    enrolled.put(oauth(7, 'syn-refresh-native'))
    assert dcc.deliver(request(8))['status'] == 'diverged'
    assert enrolled.doc()['claudeAiOauth']['refreshToken'] == 'syn-refresh-native'
    assert (enrolled.managed / dcc.STATE_NAME).read_bytes() == state
    assert not list(enrolled.store.glob('*.lock')) and not list(enrolled.home.glob('*.lock'))
    assert before != enrolled.raw()  # only the test's own edit changed the file
    # Observe mode is not an enabled consumer: the ACK set is the managed-only one.
    assert dcc.current_set(True) == dcc.consumer_set(True, None)


# ------------------------------------------------------------ delivery

def test_default_only_delivery_preserves_unrelated_entries_and_is_digest_only(writing):
    result = dcc.deliver(request(8))
    assert result == {'status': 'materialized', 'enabled': True}
    document = writing.doc()
    assert document['claudeAiOauth'] == oauth(8)
    assert document['mcpOAuth'] == MCP['mcpOAuth'] and document['other'] == [1, 2]
    state = writing.state()
    assert state['committed']['generation'] == 8 and state['pending'] is None
    assert state['committed']['oauth_digest'] == native.oauth_digest(oauth(8))
    no_secret((writing.managed / dcc.STATE_NAME).read_text())
    assert dcc.deliver(request(8)) == {'status': 'in_step', 'enabled': True}
    assert dcc.deliver(request(9))['status'] == 'materialized'
    assert not list(writing.store.glob('*.lock')) and not list(writing.home.glob('*.lock'))
    assert oct(writing.cred.stat().st_mode & 0o777) == '0o600'
    assert not [p for p in writing.store.iterdir() if p.name.endswith('.tmp')]


def test_compact_document_stays_compact(writing):
    writing.put(oauth(7), raw=json.dumps({'claudeAiOauth': oauth(7), 'k': 1}, separators=(',', ':')).encode())
    dcc.deliver(request(8))
    assert b'\n' not in writing.raw() and writing.doc()['k'] == 1


def test_helper_delivers_default_only_without_managed_profile_or_receipt(writing, capsys):
    stdin = io.BytesIO(json.dumps(request(8)).encode())
    code = deliver.main(['/nonexistent/hub.pyz', 'materialize'], stdin)
    out = capsys.readouterr()
    result = json.loads(out.out)
    assert code == 0
    assert result == {'status': 'absent', 'default': 'materialized', 'default_enabled': True,
                      'consumer_set': dcc.current_set(False)}
    assert result['consumer_set'] != dcc.current_set(True)
    no_secret(out.out, out.err)
    assert writing.doc()['claudeAiOauth'] == oauth(8)
    code = deliver.main(['/nonexistent/hub.pyz', 'check'], io.BytesIO(json.dumps(request(8)).encode()))
    assert code == 0 and json.loads(capsys.readouterr().out)['default'] == 'in_step'
    code = deliver.main(['/nonexistent/hub.pyz', 'check'], io.BytesIO(json.dumps(request(9)).encode()))
    assert json.loads(capsys.readouterr().out)['default'] == 'pending'
    assert writing.doc()['claudeAiOauth'] == oauth(8)  # check never writes


def test_a_new_consumer_at_the_current_generation_changes_the_consumer_set(enrolled):
    managed_only = dcc.current_set(True)
    enrolled.write_mode()
    assert dcc.current_set(True) != managed_only
    assert dcc.deliver(request(7)) == {'status': 'in_step', 'enabled': True}


def test_foreign_local_oauth_is_diverged_with_zero_write_and_zero_upload(writing):
    # A refresh or an account switch cannot be told apart from the bytes: both diverge.
    for body in (oauth(7, 'syn-refresh-native'), {'accessToken': 'syn-native-other-account', 'refreshToken': 'syn-native-r',
                                                  'expiresAt': EXPIRES}, dict(oauth(7), expiresAt=EXPIRES + 10**6)):
        writing.put(body)
        before = writing.raw()
        state = (writing.managed / dcc.STATE_NAME).read_bytes()
        assert dcc.deliver(request(8)) == {'status': 'diverged', 'enabled': True}
        assert writing.raw() == before
        assert (writing.managed / dcc.STATE_NAME).read_bytes() == state
        assert dcc.deliver(request(8), check_only=True)['status'] == 'diverged'
    assert not list(writing.store.glob('*.lock'))  # locks released; socket.connect would have failed the test


def test_older_and_equal_but_different_generations_are_refused(writing):
    dcc.deliver(request(9))
    after = writing.raw()
    assert dcc.deliver(request(8))['status'] == 'generation_refused'
    changed = request(9, body=dict(oauth(9), accessToken='syn-access-changed'))
    assert dcc.deliver(changed)['status'] == 'generation_refused'
    assert writing.raw() == after
    assert dcc.deliver(request(9)) == {'status': 'in_step', 'enabled': True}


def test_revision_change_and_unsupported_protocol_block_only_this_consumer(writing):
    assert dcc.deliver(request(8, revision='44444444-4444-4444-8444-444444444444'))['status'] == 'revision_changed'
    path = writing.managed / dcc.STATE_NAME
    state = json.loads(path.read_text())
    path.write_text(json.dumps(dict(state, protocol='claude-file-store-locks-v0')))
    before = writing.raw()
    assert dcc.deliver(request(8)) == {'status': 'protocol_unsupported', 'enabled': True}
    assert writing.raw() == before
    # The managed step is independent: the helper still answers, with a fixed default diagnostic.
    result = deliver.deliver('/nonexistent/hub.pyz', request(8))
    assert result['status'] == 'absent' and result['default'] == 'protocol_unsupported'
    path.write_text(json.dumps(dict(state, mode='write', pending={'generation': 1})))
    assert dcc.deliver(request(8))['status'] == 'enrollment_invalid'


def test_malformed_or_untrusted_store_at_runtime_refuses_without_write(writing):
    good = writing.raw()
    cases = {}
    writing.cred.write_bytes(b'[]')
    cases['malformed'] = dcc.deliver(request(8))['status']
    writing.cred.write_bytes(good)
    real = writing.tmp / 'real.json'
    real.write_bytes(good)
    writing.cred.unlink()
    writing.cred.symlink_to(real)
    cases['symlink'] = dcc.deliver(request(8))['status']
    writing.cred.unlink()
    os.link(real, writing.cred)
    cases['hardlink'] = dcc.deliver(request(8))['status']
    writing.cred.unlink()
    cases['missing'] = dcc.deliver(request(8))['status']
    assert cases == {'malformed': 'store_malformed', 'symlink': 'store_untrusted',
                     'hardlink': 'store_untrusted', 'missing': 'store_missing'}
    assert real.read_bytes() == good and not list(writing.store.glob('*.lock'))


def test_changed_store_path_is_refused_and_runtime_ignores_ambient_environment(writing, monkeypatch):
    other = writing.tmp / 'ambient'
    other.mkdir(mode=0o700)
    (other / '.credentials.json').write_text(json.dumps({'claudeAiOauth': oauth(7)}))
    monkeypatch.setenv('CLAUDE_SECURESTORAGE_CONFIG_DIR', str(other))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(other))
    untouched = (other / '.credentials.json').read_bytes()
    assert dcc.deliver(request(8))['status'] == 'materialized'
    assert (other / '.credentials.json').read_bytes() == untouched
    assert writing.doc()['claudeAiOauth'] == oauth(8)
    # The pinned directory replaced by another one (same path, new identity).
    moved = writing.tmp / 'moved'
    writing.store.rename(moved)
    writing.store.mkdir(mode=0o700)
    (writing.store / '.credentials.json').write_text(json.dumps({'claudeAiOauth': oauth(8)}))
    assert dcc.deliver(request(9))['status'] == 'store_changed'
    assert json.loads((writing.store / '.credentials.json').read_text())['claudeAiOauth'] == oauth(8)


# ----------------------------------------------------- crash windows

class Crash(BaseException):
    pass


def crash_at(monkeypatch, point):
    def fault(name):
        if name == point:
            raise Crash(name)
    monkeypatch.setattr(dcc, 'FAULT', fault)


def test_crash_after_intent_before_file_reconciles_forward(writing, monkeypatch):
    original = writing.raw()
    crash_at(monkeypatch, 'after_intent')
    with pytest.raises(Crash):
        dcc.deliver(request(8))
    state = writing.state()
    assert writing.raw() == original and state['committed']['generation'] == 7
    assert state['pending'] == {'generation': 8, 'from_digest': native.oauth_digest(oauth(7)),
                                'to_digest': native.oauth_digest(oauth(8))}
    no_secret(json.dumps(state))
    assert not list(writing.store.glob('*.lock')) and not list(writing.home.glob('*.lock'))
    monkeypatch.setattr(dcc, 'FAULT', None)
    assert dcc.deliver(request(8))['status'] == 'materialized'
    assert writing.doc()['claudeAiOauth'] == oauth(8) and writing.state()['pending'] is None


def test_crash_after_file_before_state_completes_forward_and_never_rolls_back(writing, monkeypatch):
    crash_at(monkeypatch, 'after_file')
    with pytest.raises(Crash):
        dcc.deliver(request(8))
    assert writing.doc()['claudeAiOauth'] == oauth(8) and writing.state()['committed']['generation'] == 7
    monkeypatch.setattr(dcc, 'FAULT', None)
    # An older generation arriving now must not replace the newer content.
    assert dcc.deliver(request(7))['status'] == 'generation_refused'
    assert dcc.deliver(request(7), check_only=True)['status'] == 'generation_refused'
    assert writing.doc()['claudeAiOauth'] == oauth(8)
    # The same generation completes the commit without rewriting.
    mtime = writing.cred.stat().st_mtime_ns
    assert dcc.deliver(request(8)) == {'status': 'in_step', 'enabled': True}
    assert writing.cred.stat().st_mtime_ns == mtime
    assert writing.state()['committed']['generation'] == 8 and writing.state()['pending'] is None


def test_crash_after_file_then_a_newer_generation_moves_forward(writing, monkeypatch):
    crash_at(monkeypatch, 'after_file')
    with pytest.raises(Crash):
        dcc.deliver(request(8))
    monkeypatch.setattr(dcc, 'FAULT', None)
    assert dcc.deliver(request(9))['status'] == 'materialized'
    assert writing.doc()['claudeAiOauth'] == oauth(9) and writing.state()['committed']['generation'] == 9


# ---------------------------------------------------- native lock contenders

EMULATOR = textwrap.dedent('''
    import json, os, sys, time
    store, mode, ready, go, tag = sys.argv[1:6]
    legacy = store + '.lock'
    def take(path, limit=20.0):
        end = time.time() + limit
        while True:
            try:
                os.mkdir(path)
                return
            except FileExistsError:
                if time.time() > end:
                    sys.exit(5)
                time.sleep(0.02)
    held = []
    def lock(path):
        take(path)
        held.append(path)
    def unlock():
        while held:
            os.rmdir(held.pop())
    if mode == 'refresh':   # native refresh: refresh lock, legacy lock, HTTP, then storage mutate
        lock(os.path.join(store, '.oauth_refresh.lock'))
        lock(legacy)
    open(ready, 'w').write('1')
    end = time.time() + 20
    while not os.path.exists(go):
        if time.time() > end:
            sys.exit(6)
        time.sleep(0.02)
    lock(os.path.join(store, '.storage-write.lock'))
    path = os.path.join(store, '.credentials.json')
    document = json.load(open(path))
    current = document['claudeAiOauth'].get('refreshToken')
    code = 0
    if current == '' or current == sys.argv[6]:
        document['claudeAiOauth'] = {'accessToken': 'syn-native-' + tag, 'refreshToken': 'syn-native-r-' + tag,
                                     'expiresAt': 4102444800000}
        tmp = path + '.' + tag + '.tmp'
        with open(tmp, 'w') as handle:
            json.dump(document, handle)
        os.replace(tmp, path)
    else:
        code = 4
    unlock()
    sys.exit(code)
''')


def spawn_emulator(world, mode, tag, expect=''):
    script = world.tmp / 'emulator.py'
    script.write_text(EMULATOR)
    ready, go = world.tmp / ('ready-' + tag), world.tmp / ('go-' + tag)
    proc = subprocess.Popen([sys.executable, '-I', str(script), str(world.store), mode, str(ready), str(go), tag, expect])
    return proc, ready, go


def wait_for(path, limit=20):
    end = time.time() + limit
    while not path.exists():
        assert time.time() < end, 'timed out'
        time.sleep(0.02)


def consumer_process(world, generation, wait='5', delay=0):
    code = textwrap.dedent('''
        import json, sys, time
        sys.path.insert(0, %r)
        from agent_hub_host import default_claude_consumer as dcc
        time.sleep(%s)
        dcc.NATIVE_WAIT_S = %s
        print(json.dumps(dcc.deliver(%r)))
    ''') % (str(REPO / 'host'), delay, wait, request(generation))
    return subprocess.Popen([sys.executable, '-I', '-c', code], stdout=subprocess.PIPE)


def test_inflight_native_refresh_blocks_the_write_then_its_rotation_is_never_overwritten(writing):
    emulator, ready, go = spawn_emulator(writing, 'refresh', 'a')
    wait_for(ready)  # a refresh is in flight: it holds the refresh and legacy locks
    before = writing.raw()
    state = (writing.managed / dcc.STATE_NAME).read_bytes()
    assert dcc.deliver(request(8)) == {'status': 'lock_busy', 'enabled': True}
    assert writing.raw() == before and (writing.managed / dcc.STATE_NAME).read_bytes() == state
    # Our attempt left the native locks exactly as it found them.
    assert (writing.store / '.oauth_refresh.lock').is_dir() and (writing.home / '.claude.lock').is_dir()
    assert not (writing.store / '.storage-write.lock').exists()
    go.write_text('1')
    assert emulator.wait(30) == 0
    assert writing.doc()['claudeAiOauth']['accessToken'] == 'syn-native-a'
    assert dcc.deliver(request(8))['status'] == 'diverged'
    assert writing.doc()['claudeAiOauth']['accessToken'] == 'syn-native-a' and writing.doc()['mcpOAuth'] == MCP['mcpOAuth']


def test_native_refresh_first_and_consumer_first_orders_both_finish_without_deadlock(writing):
    # Order 1: the native refresh takes its locks first, the consumer arrives while they are held.
    emulator, ready, go = spawn_emulator(writing, 'refresh', 'b')
    wait_for(ready)
    consumer = consumer_process(writing, 8, wait='15')
    time.sleep(1.0)
    go.write_text('1')
    assert emulator.wait(30) == 0
    out, _ = consumer.communicate(timeout=60)
    assert json.loads(out)['status'] == 'diverged'
    assert writing.doc()['claudeAiOauth']['accessToken'] == 'syn-native-b'
    assert not list(writing.store.glob('.*.lock')) and not (writing.home / '.claude.lock').exists()


def test_consumer_first_holds_all_three_locks_native_contenders_wait_and_cas_loses_nothing(writing):
    hold = writing.tmp / 'hold'
    started = writing.tmp / 'started'
    code = textwrap.dedent('''
        import os, time, sys
        sys.path.insert(0, %r)
        from agent_hub_host import native_claude_store as n
        with n.NativeLocks(%r, wait_s=10):
            open(%r, 'w').write('1')
            end = time.time() + 20
            while not os.path.exists(%r) and time.time() < end:
                time.sleep(0.02)
    ''') % (str(REPO / 'host'), str(writing.store), str(started), str(hold))
    holder = subprocess.Popen([sys.executable, '-I', '-c', code])
    wait_for(started)
    # Both native lock orders contend while the consumer holds refresh+legacy+storage.
    refresh, ready_r, go_r = spawn_emulator(writing, 'refresh', 'c')
    storage, ready_s, go_s = spawn_emulator(writing, 'storage', 'd')
    time.sleep(0.5)
    assert not ready_r.exists()      # refresh-order native is blocked on the refresh lock
    assert ready_s.exists()          # storage-only native only needs the storage lock at mutate time
    go_s.write_text('1')
    time.sleep(0.5)
    assert storage.poll() is None    # ... and is blocked there by our storage lock
    hold.write_text('1')
    assert holder.wait(30) == 0
    wait_for(ready_r)
    go_r.write_text('1')
    assert refresh.wait(30) in (0, 4) and storage.wait(30) in (0, 4)
    document = writing.doc()          # always a whole, valid document with unrelated entries intact
    assert document['mcpOAuth'] == MCP['mcpOAuth'] and document['claudeAiOauth']['accessToken'].startswith('syn-native-')
    assert not list(writing.store.glob('.*.lock')) and not (writing.home / '.claude.lock').exists()


def test_concurrent_rounds_never_lose_unrelated_entries_or_corrupt_the_document(writing):
    for index in range(3):
        emulator, ready, go = spawn_emulator(writing, 'refresh', 'r%d' % index, expect=writing.doc()['claudeAiOauth']['refreshToken'])
        consumer = consumer_process(writing, 8 + index, wait='15')
        wait_for(ready)
        go.write_text('1')
        emulator.wait(40)
        out, _ = consumer.communicate(timeout=60)
        status = json.loads(out)['status']
        assert status in ('materialized', 'diverged', 'in_step', 'generation_refused', 'lock_busy'), status
        document = writing.doc()
        assert document['mcpOAuth'] == MCP['mcpOAuth']
        assert document['claudeAiOauth']['accessToken'] in {'syn-native-r%d' % index, 'syn-access-%d' % (8 + index)}
        # Reset to a state the consumer knows so the next round starts clean.
        writing.put(oauth(8 + index))
        state = writing.state()
        path = writing.managed / dcc.STATE_NAME
        path.write_text(json.dumps(dict(state, committed={'generation': 8 + index, 'oauth_digest': native.oauth_digest(oauth(8 + index))},
                                        pending=None)))
    assert not list(writing.store.glob('.*.lock'))


def test_foreign_stale_and_busy_locks_are_never_removed(writing):
    stale = writing.store / '.storage-write.lock'
    stale.mkdir()
    old = time.time() - 3600
    os.utime(stale, (old, old))
    before = writing.raw()
    assert dcc.deliver(request(8)) == {'status': 'lock_stale_operator_recovery', 'enabled': True}
    assert stale.is_dir() and abs(stale.stat().st_mtime - old) < 2  # untouched, not "refreshed"
    assert writing.raw() == before
    # Our earlier refresh/legacy locks were released; only the foreign one remains.
    assert not (writing.store / '.oauth_refresh.lock').exists() and not (writing.home / '.claude.lock').exists()
    stale.rmdir()
    foreign = writing.home / '.claude.lock'
    foreign.mkdir()
    os.utime(foreign, (old, old))
    assert dcc.deliver(request(8))['status'] == 'lock_stale_operator_recovery' and foreign.is_dir()
    foreign.rmdir()
    fresh = writing.store / '.oauth_refresh.lock'
    fresh.mkdir()
    assert dcc.deliver(request(8))['status'] == 'lock_busy' and fresh.is_dir()


def test_release_removes_only_the_incarnation_it_created_and_heartbeat_keeps_the_lock_fresh(world, monkeypatch):
    world.put(oauth(7))
    paths = native.lock_paths(world.store)
    monkeypatch.setattr(native, 'LOCK_ORDER', tuple((name, stale, 0.1) for name, stale, _ in native.LOCK_ORDER))
    with native.NativeLocks(world.store, wait_s=2) as locks:
        first = paths['storage'].stat().st_mtime_ns
        deadline = time.time() + 5
        while paths['storage'].stat().st_mtime_ns == first and time.time() < deadline:
            time.sleep(0.05)
        assert paths['storage'].stat().st_mtime_ns != first and not locks.compromised()
    assert not any(path.exists() for path in paths.values())
    # Another party replaced the directory: it is not ours any more and must survive our release.
    with native.NativeLocks(world.store, wait_s=2) as locks:
        paths['storage'].rmdir()
        paths['storage'].mkdir()
        assert locks.compromised()
    assert paths['storage'].is_dir()
    paths['storage'].rmdir()


def test_a_lost_lock_blocks_the_replacement(writing, monkeypatch):
    def fault(name):
        if name == 'after_intent':
            (writing.store / '.storage-write.lock').rmdir()
            (writing.store / '.storage-write.lock').mkdir()
    monkeypatch.setattr(dcc, 'FAULT', fault)
    before = writing.raw()
    assert dcc.deliver(request(8)) == {'status': 'lock_compromised', 'enabled': True}
    assert writing.raw() == before
    assert (writing.store / '.storage-write.lock').is_dir()  # the replacement survives
    assert writing.state()['pending']['generation'] == 8


# ------------------------------------------------------------ no leaks

def test_no_secret_reaches_any_output_or_state(writing, capsys):
    writing.put(oauth(7, 'syn-refresh-live'))
    stored = writing.managed / dcc.STATE_NAME
    for generation in (8, 9):
        results = [dcc.deliver(request(generation)), dcc.status(), dcc.check(lambda g=generation: view(g)),
                   deliver.deliver('/nonexistent/hub.pyz', request(generation))]
        no_secret(json.dumps(results), stored.read_text())
    code = deliver.main(['/nonexistent/hub.pyz', 'materialize'], io.BytesIO(json.dumps(request(8)).encode()))
    out = capsys.readouterr()
    assert code == 0
    no_secret(out.out, out.err)
    code = deliver.main(['/nonexistent/hub.pyz', 'materialize'], io.BytesIO(b'{"files": "syn-access-leak"}'))
    out = capsys.readouterr()
    assert code == 3 and json.loads(out.out) == {'reason': 'invalid_request', 'status': 'refused'}
    no_secret(out.out, out.err)


def test_check_is_read_only_and_disable_leaves_credentials_alone(writing):
    before = writing.raw()
    result = dcc.check(lambda: view(8))
    assert result == {'status': 'pending', 'enabled': True, 'generation': 8} and writing.raw() == before
    assert dcc.disable()['removed'] is True
    assert writing.raw() == before and dcc.deliver(request(8)) == {'status': 'absent'}
    assert dcc.disable() == {'enrolled': False, 'removed': False}


# ------------------------------------------- lock ownership at success / strict JSON

def lose_lock(world):
    lock = world.store / '.storage-write.lock'
    lock.rmdir()
    lock.mkdir()


def test_lock_lost_before_an_already_held_success_is_not_claimed(writing, monkeypatch):
    assert dcc.deliver(request(8))['status'] == 'materialized'
    monkeypatch.setattr(dcc, 'FAULT', lambda name: lose_lock(writing) if name == 'held' else None)
    path = writing.managed / dcc.STATE_NAME
    before = path.read_bytes()
    assert dcc.deliver(request(8)) == {'status': 'lock_compromised', 'enabled': True}
    assert path.read_bytes() == before
    assert (writing.store / '.storage-write.lock').is_dir()      # the replacement is not ours: left alone
    (writing.store / '.storage-write.lock').rmdir()
    monkeypatch.setattr(dcc, 'FAULT', None)
    assert dcc.deliver(request(8))['status'] == 'in_step'


def test_lock_lost_after_the_replacement_is_not_committed_or_claimed(writing, monkeypatch):
    monkeypatch.setattr(dcc, 'FAULT', lambda name: lose_lock(writing) if name == 'after_file' else None)
    assert dcc.deliver(request(8)) == {'status': 'lock_compromised', 'enabled': True}
    # The bytes are already the new generation, but the state is not committed and nothing is claimed.
    assert writing.doc()['claudeAiOauth'] == oauth(8)
    state = writing.state()
    assert state['committed']['generation'] == 7 and state['pending']['generation'] == 8
    assert dcc.deliver(request(8), check_only=True)['status'] == 'pending'
    (writing.store / '.storage-write.lock').rmdir()
    monkeypatch.setattr(dcc, 'FAULT', None)
    assert dcc.deliver(request(8)) == {'status': 'in_step', 'enabled': True}
    assert writing.state()['committed']['generation'] == 8 and writing.state()['pending'] is None


@pytest.mark.parametrize('constant', ['NaN', 'Infinity', '-Infinity'])
def test_non_finite_json_is_malformed_everywhere_native_json_parse_would_reject_it(writing, constant):
    before_state = (writing.managed / dcc.STATE_NAME).read_bytes()
    # An unrelated field of the existing document: refuse, never rewrite it.
    writing.cred.write_text('{"claudeAiOauth": %s, "mcpOAuth": {"x": %s}}' % (json.dumps(oauth(7)), constant))
    raw = writing.raw()
    assert dcc.deliver(request(8)) == {'status': 'store_malformed', 'enabled': True}
    assert dcc.deliver(request(8), check_only=True)['status'] == 'store_malformed'
    assert writing.raw() == raw and (writing.managed / dcc.STATE_NAME).read_bytes() == before_state
    # The incoming central payload.
    writing.put(oauth(7))
    bad = request(8)
    bad['files'][PATH] = '{"claudeAiOauth": {"accessToken": "syn-access-8", "expiresAt": %s}}' % constant
    assert dcc.deliver(bad)['status'] == 'invalid_request'
    with pytest.raises(dcc.Refused):
        dcc.enroll(lambda: dict(view(7), files={PATH: bad['files'][PATH]}), confirm_same_account=True)
    # Enrollment against a store holding it is refused as well.
    dcc.disable()
    writing.cred.write_text('{"claudeAiOauth": %s, "k": %s}' % (json.dumps(oauth(7)), constant))
    with pytest.raises(dcc.Refused, match='store_malformed'):
        writing.enroll(7)
    with pytest.raises(native.StoreRefused):
        native.render_document({'claudeAiOauth': oauth(7), 'k': float(constant.lower().replace('infinity', 'inf'))}, None)


# ------------------------------- B2: no automatic crash reaping of native locks

def test_a_crashed_owner_leaves_locks_that_are_refused_never_reaped(writing):
    code = textwrap.dedent('''
        import os, sys
        sys.path.insert(0, %r)
        from agent_hub_host import default_claude_consumer as dcc
        def fault(name):
            if name == 'after_file':
                os._exit(9)
        dcc.FAULT = fault
        dcc.deliver(%r)
    ''') % (str(REPO / 'host'), request(8))
    assert subprocess.run([sys.executable, '-I', '-c', code], capture_output=True, timeout=60).returncode == 9
    locks = [writing.store / '.oauth_refresh.lock', writing.home / '.claude.lock', writing.store / '.storage-write.lock']
    assert all(lock.is_dir() for lock in locks)
    # Fresh: indistinguishable from a live native refresh -> busy. Never removed.
    assert dcc.deliver(request(8)) == {'status': 'lock_busy', 'enabled': True}
    old = time.time() - 3600
    for lock in locks:
        os.utime(lock, (old, old))
    # Stale: an operator decision, reported truthfully; no automatic hard-crash recovery.
    assert dcc.deliver(request(8)) == {'status': 'lock_stale_operator_recovery', 'enabled': True}
    assert all(lock.is_dir() for lock in locks)
    assert writing.state()['committed']['generation'] == 7 and writing.state()['pending']['generation'] == 8
    # After the operator removes them the pending intent completes forward.
    for lock in locks:
        lock.rmdir()
    assert dcc.deliver(request(8)) == {'status': 'in_step', 'enabled': True}
    assert writing.state()['committed']['generation'] == 8


def test_a_foreign_replacement_between_staleness_check_and_any_removal_is_preserved(writing, monkeypatch):
    lock = writing.store / '.storage-write.lock'
    lock.mkdir()
    old = time.time() - 3600
    os.utime(lock, (old, old))
    real_lstat, real_rmdir, removed, state = os.lstat, os.rmdir, [], {'swapped': False}

    def lstat(path, *a, **kw):
        result = real_lstat(path, *a, **kw)
        if Path(path) == lock and not state['swapped']:
            # The deterministic race: a native contender replaces the directory right after we looked.
            state['swapped'] = True
            real_rmdir(lock)  # the contender's own removal, not ours
            lock.mkdir()
            (lock / 'foreign-marker').write_text('live native lock')
        return result
    monkeypatch.setattr(os, 'lstat', lstat)
    monkeypatch.setattr(os, 'rmdir', lambda path, *a, **kw: (removed.append(str(path)), real_rmdir(path, *a, **kw))[1])
    assert dcc.deliver(request(8))['status'] in ('lock_stale_operator_recovery', 'lock_busy')
    assert state['swapped'] and (lock / 'foreign-marker').read_text() == 'live native lock'
    assert str(lock) not in removed          # we never removed a directory we did not create


# ------------------------------------------ B3: durable disable

def test_disable_fsyncs_the_directory_after_the_unlink_and_surfaces_failure(writing, monkeypatch):
    path = writing.managed / dcc.STATE_NAME
    real_fsync, events = os.fsync, []

    def fsync(fd):
        try:
            same_dir = os.path.samefile('/proc/self/fd/%d' % fd, writing.managed)
        except OSError:
            same_dir = False
        events.append(('dir' if same_dir else 'file', path.exists()))
        return real_fsync(fd)
    monkeypatch.setattr(os, 'fsync', fsync)
    assert dcc.disable() == {'enrolled': False, 'removed': True}
    # The containing directory was synced, and only after the enrollment was unlinked.
    assert ('dir', False) in events and ('dir', True) not in events
    # A sync failure is a failure, not a quiet success; repeating the command succeeds.
    path.write_bytes(b'{}')
    monkeypatch.setattr(os, 'fsync', lambda fd: (_ for _ in ()).throw(OSError(5, 'io')))
    with pytest.raises(dcc.Refused, match='disable_not_durable'):
        dcc.disable()
    monkeypatch.setattr(os, 'fsync', real_fsync)
    assert dcc.disable() == {'enrolled': False, 'removed': False}


def test_disable_failure_is_a_command_error_and_never_claims_success(writing, monkeypatch):
    monkeypatch.setattr(os, 'fsync', lambda fd: (_ for _ in ()).throw(OSError(5, 'io')))
    with pytest.raises(ValueError, match='disable_not_durable'):
        dcc.command(argparse.Namespace(default_claude_cmd='disable'))


# --------------------------------- B4: executable and version binding

def test_enrollment_binds_the_resolved_executable_and_supported_version(world):
    world.put(oauth(7))
    world.enroll(7)
    pin = world.state()['executable']
    assert pin['path'] == str(world.claude.resolve()) and pin['version'] == '2.1.322'
    assert dcc.status()['claude_version'] == '2.1.322'
    no_secret(json.dumps(world.state()))


@pytest.mark.parametrize('case', ['missing', 'unsupported', 'unparsable', 'failing', 'group_writable'])
def test_enrollment_refuses_a_missing_unsupported_or_untrusted_executable_without_mutation(world, case, monkeypatch):
    world.put(oauth(7))
    if case == 'missing':
        world.claude.unlink()
        monkeypatch.setenv('PATH', str(world.bin))
        expect = 'executable_missing'
    elif case == 'unsupported':
        world.set_version('2.1.999')
        expect = 'version_unsupported'
    elif case == 'unparsable':
        world.set_version('no version here')
        expect = 'version_unavailable'
    elif case == 'failing':
        world.set_version('2.1.322', code=1)
        expect = 'version_unavailable'
    else:
        os.chmod(world.claude, 0o775)
        expect = 'executable_untrusted'
    before = world.raw()
    with pytest.raises(dcc.Refused, match=expect):
        world.enroll(7)
    assert world.raw() == before and not (world.managed / dcc.STATE_NAME).exists()


@pytest.mark.parametrize('change,expect', [
    ('unsupported', 'version_unsupported'), ('replaced', 'executable_changed'),
    ('removed', 'executable_missing'), ('untrusted', 'executable_untrusted')])
def test_changed_executable_refuses_this_consumer_before_any_write(writing, monkeypatch, change, expect):
    before, state = writing.raw(), (writing.managed / dcc.STATE_NAME).read_bytes()
    if change == 'unsupported':
        # Same inode/size/mtime would be required to evade the identity pin, so go through a replacement
        # and then re-pin it to isolate the version gate.
        writing.set_version('2.1.999')
        pin = dict(writing.state()['executable'], **native._executable_identity(str(writing.claude)))
        saved = json.loads(json.dumps(writing.state()))
        saved['executable'] = dict(pin, version='2.1.322')
        (writing.managed / dcc.STATE_NAME).write_text(json.dumps(saved))
        state = (writing.managed / dcc.STATE_NAME).read_bytes()
    elif change == 'replaced':
        writing.set_version('2.1.322')
    elif change == 'removed':
        writing.claude.unlink()
    else:
        os.chmod(writing.claude, 0o777)
    for check_only in (False, True):
        assert dcc.deliver(request(8), check_only=check_only) == {'status': expect, 'enabled': True}
    assert dcc.status()['local'] == expect
    assert writing.raw() == before and (writing.managed / dcc.STATE_NAME).read_bytes() == state
    # Only this consumer is blocked: it never claims a held generation, so no ACK can cover it.
    assert dcc.deliver(request(8))['status'] not in dcc.HELD
    assert not list(writing.store.glob('.*.lock'))


def test_version_drift_to_another_supported_version_refuses_until_reenrolled(writing, monkeypatch):
    monkeypatch.setattr(native, 'SUPPORTED_VERSIONS', ('2.1.322', '2.1.323'))
    # Same file, but it now reports another version: the pin (and not just support) is checked.
    code = (writing.bin / 'claude').read_text().replace('2.1.322', '2.1.323')
    os.chmod(writing.claude, 0o755)
    state = json.loads(json.dumps(writing.state()))
    (writing.bin / 'claude').write_text(code)
    state['executable'].update(native._executable_identity(str(writing.claude)))
    (writing.managed / dcc.STATE_NAME).write_text(json.dumps(state))
    assert dcc.deliver(request(8)) == {'status': 'version_changed', 'enabled': True}
    assert writing.doc()['claudeAiOauth'] == oauth(7)
    # Re-enrollment (the operator's recovery) re-pins the new supported version; same store, known content.
    writing.enroll(7)
    assert writing.state()['executable']['version'] == '2.1.323' and writing.state()['mode'] == 'write'
    assert dcc.deliver(request(8))['status'] == 'materialized'


def test_runtime_uses_the_pinned_executable_not_the_ambient_path(writing, monkeypatch):
    other = writing.tmp / 'other-bin'
    other.mkdir()
    fake = other / 'claude'
    fake.write_text('#!/bin/sh\necho 9.9.9\n')
    os.chmod(fake, 0o755)
    monkeypatch.setenv('PATH', str(other) + os.pathsep + os.environ['PATH'])
    assert dcc.deliver(request(8))['status'] == 'materialized'
