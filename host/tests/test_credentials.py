"""Intake 5814e109: Hub-owned credentials on the host, quse-free admission.

- credentials.reconcile materializes Hub's generation, writes back a local
  engine rotation with CAS, adopts Hub's copy when refused and preserves the
  live local copy when Hub is unreachable.
- A credential generation change never reinstalls the engine.
- execute()/admission()/native_admission/collect_subscriptions/quotas() never
  invoke quse; Hub's admission verdict decides.
- A brand-new host gets every account from one sync (no per-host login).
"""
import io
import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from agent_hub_host import credentials as c
from agent_hub_host import provisioning as p

LABEL = 'codex-orin'
REV = '42d8580e-3394-443a-b834-c5a944f30614'
PATH = '.codex/auth.json'


def auth(tag, last_refresh='2026-10-28T15:56:27Z'):
    return json.dumps({'auth_mode': 'chatgpt', 'last_refresh': last_refresh,
                       'tokens': {'access_token': 'acc-' + tag, 'refresh_token': 'refresh-' + tag, 'account_id': 'acct'}})


def bundle(gen, content, **extra):
    b = {'label': LABEL, 'provider': 'codex', 'version': '0.162.1', 'revision': REV,
         'files': {PATH: content, '.codex/config.toml': 'approval_policy = "never"\n'}, 'env': {}, 'args': [],
         'credential_generation': gen, 'credential_paths': [PATH], 'credential_status': 'ok'}
    b.update(extra)
    return b


class FakeHub:
    """Hub credential endpoint with CAS semantics (mirrors src/provisioning.ts)."""

    def __init__(self, gen, files):
        self.gen, self.files, self.calls = gen, dict(files), []
        self.down = False

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if self.down:
            raise OSError('unreachable')
        if path.endswith('/credential') and method == 'POST':
            def fresh(v):
                return json.loads(v[PATH])['last_refresh']
            accept = payload['base_generation'] == self.gen or fresh(payload['files']) > fresh(self.files)
            if accept:
                self.gen += 1
                self.files.update(payload['files'])
            return {'label': LABEL, 'generation': self.gen, 'status': 'ok', 'files': dict(self.files), 'accepted': accept}
        if path.endswith('/credential') and method == 'GET':
            return {'label': LABEL, 'generation': self.gen, 'status': 'ok', 'files': dict(self.files)}
        if path == '/api/engines/admission':
            return {'launch_allowed': True, 'profile_supported': True, 'account_label': payload['label'], 'observed_at': '2026-10-29T00:00:00Z'}
        raise AssertionError(path)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR', str(tmp_path / 'managed'))
    dest = p.root() / (LABEL + '-' + REV)
    h = dest / 'home'
    (h / '.codex').mkdir(parents=True, mode=0o700)
    for d in (p.root(), dest, h, h / '.codex'):
        d.chmod(0o700)
    return dest, h


def read(h):
    return (h / PATH).read_text()


def test_materializes_generation_and_stays_in_step(home):
    dest, h = home
    hub = FakeHub(1, {PATH: auth('a')})
    assert c.reconcile(LABEL, bundle(1, auth('a')), h, dest, hub)['status'] == 'materialized'
    assert read(h) == auth('a')
    assert c.reconcile(LABEL, bundle(1, auth('a')), h, dest, hub)['status'] == 'in_step'
    # Hub refreshed centrally: the next sync carries generation 2 and replaces the file.
    assert c.reconcile(LABEL, bundle(2, auth('b')), h, dest, hub)['status'] == 'materialized'
    assert read(h) == auth('b')
    assert hub.calls == []  # no write-back: the host never refreshed anything itself
    state = json.loads((dest / c.STATE_FILE).read_text())
    assert state['generation'] == 2 and 'refresh-b' not in json.dumps(state)  # digests only


def test_local_engine_rotation_is_written_back_and_other_host_converges(home, tmp_path):
    dest, h = home
    hub = FakeHub(1, {PATH: auth('a')})
    c.reconcile(LABEL, bundle(1, auth('a')), h, dest, hub)
    (h / PATH).write_text(auth('rotated', '2026-10-29T03:00:00Z'))  # the engine refreshed on this host
    result = c.reconcile(LABEL, bundle(1, auth('a')), h, dest, hub)
    assert result == {'status': 'written_back', 'generation': 2}
    assert hub.calls[-1][2]['base_generation'] == 1
    assert read(h) == auth('rotated', '2026-10-29T03:00:00Z')
    # A second host (still at generation 1, same old copy) adopts the rotation on its next sync.
    other = tmp_path / 'other'
    (other / 'home' / '.codex').mkdir(parents=True)
    for d in (other, other / 'home', other / 'home' / '.codex'):
        d.chmod(0o700)
    c.reconcile(LABEL, bundle(hub.gen, hub.files[PATH]), other / 'home', other, hub)
    assert (other / 'home' / PATH).read_text() == auth('rotated', '2026-10-29T03:00:00Z')


def test_refused_stale_copy_adopts_hub_and_unreachable_hub_keeps_live_copy(home):
    dest, h = home
    hub = FakeHub(3, {PATH: auth('live', '2026-10-28T15:56:27Z')})
    # Upgrade path: no local state, a stale divergent copy (the Tabo 401 case).
    (h / PATH).write_text(auth('stale', '2026-10-18T16:01:14Z'))
    result = c.reconcile(LABEL, bundle(3, hub.files[PATH]), h, dest, hub)
    assert result['status'] == 'adopted_hub'
    assert hub.calls[-1][2]['base_generation'] == -1
    assert read(h) == auth('live', '2026-10-28T15:56:27Z')
    # Rotated locally while Hub is unreachable: nothing is overwritten.
    (h / PATH).write_text(auth('newer', '2026-10-29T04:00:00Z'))
    hub.down = True
    assert c.reconcile(LABEL, bundle(3, hub.files[PATH]), h, dest, hub)['status'] == 'writeback_pending'
    assert read(h) == auth('newer', '2026-10-29T04:00:00Z')


def test_generation_change_is_not_a_reinstall():
    old = bundle(1, auth('a'))
    receipt = {k: v for k, v in old.items() if k not in c.OVERLAY_KEYS}  # written before Hub owned credentials
    new = bundle(7, auth('z'))
    paths = new['credential_paths']
    assert c.install_snapshot(receipt, paths) == c.install_snapshot(new, paths)
    assert c.install_snapshot(dict(new, version='0.163.0'), paths) != c.install_snapshot(receipt, paths)


def test_install_same_snapshot_reconciles_without_npm(home, monkeypatch):
    dest, h = home
    first = bundle(1, auth('a'))
    p.write_json(dest / 'bundle.json', {k: v for k, v in first.items() if k not in c.OVERLAY_KEYS})
    monkeypatch.setattr(p, 'install_aplexer_state_hooks', lambda *a, **k: None)
    monkeypatch.setattr(p, 'install_managed_engine_permissions', lambda *a, **k: None)

    def no_subprocess(*a, **k):
        raise AssertionError('reinstall attempted')
    monkeypatch.setattr(p.subprocess, 'run', no_subprocess)
    hub = FakeHub(2, {PATH: auth('b')})
    p.install(bundle(2, auth('b')), hub)
    assert read(h) == auth('b')
    assert p.read_json(dest / 'bundle.json')['credential_generation'] == 2


def _no_quse(monkeypatch):
    real = subprocess.run

    def guard(argv, *a, **k):
        if any('quse' in str(x) for x in (argv if isinstance(argv, (list, tuple)) else [argv])):
            raise AssertionError('quse invoked')
        return real(argv, *a, **k)
    monkeypatch.setattr(subprocess, 'run', guard)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: (_ for _ in ()).throw(AssertionError('subprocess spawned')))


def test_hub_launch_gate_decides_without_quse(monkeypatch):
    _no_quse(monkeypatch)
    verdicts = iter([
        {'launch_allowed': False, 'reason': 'credential_rejected'},
        {'launch_allowed': False, 'reason': 'quota_exhausted'},
        {'launch_allowed': True, 'quota_warning': 'quota_probe_unavailable'},
        {'launch_allowed': True},
    ])
    req = lambda m, path, payload=None: next(verdicts)
    assert p.hub_launch_gate(LABEL, req) == (False, 'credential_rejected')
    assert p.hub_launch_gate(LABEL, req) == (False, 'quota_exhausted')
    assert p.hub_launch_gate(LABEL, req) == (True, 'quota_probe_warning:quota_probe_unavailable')
    assert p.hub_launch_gate(LABEL, req) == (True, 'ok')

    def down(*a, **k):
        raise OSError('down')
    assert p.hub_launch_gate(LABEL, down)[0] is True


def test_launch_fetches_current_credential(home):
    dest, h = home
    hub = FakeHub(5, {PATH: auth('current')})
    c.reconcile(LABEL, bundle(4, auth('older')), h, dest, hub)
    result = p.refresh_launch_credential(LABEL, bundle(4, auth('older')), h, dest, hub)
    assert result['generation'] == 5
    assert read(h) == auth('current')
    assert ('GET', '/api/engines/%s/credential' % LABEL, None) in hub.calls


def test_admission_has_no_local_quota_probe(monkeypatch, tmp_path, capsys):
    _no_quse(monkeypatch)
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR', str(tmp_path))
    p.write_json(tmp_path / 'state.json', [{'label': LABEL, 'revision': REV, 'provider': 'codex'}])
    p.write_json(tmp_path / (LABEL + '-' + REV) / 'bundle.json', bundle(1, auth('a')))
    monkeypatch.setattr(p.sys, 'argv', ['provisioning', LABEL])
    monkeypatch.setattr(p.sys, 'stdin', io.StringIO(''))
    monkeypatch.setattr(p, 'host_request', lambda m, path, payload=None: {
        'launch_allowed': False, 'reason': 'quota_exhausted', 'profile_supported': True, 'account_label': LABEL, 'observed_at': 'x'})
    p.admission()
    out = json.loads(capsys.readouterr().out)
    assert out['launch_allowed'] is False
    assert out['reason'] == 'codex_quota_gate_codex_reserve_required'


def test_native_admission_asks_hub_not_quse(monkeypatch):
    _no_quse(monkeypatch)
    from agent_hub_host import native_admission as na
    seen = []

    def hub(method, path, payload=None):
        seen.append((path, payload))
        return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'shared-claude'}
    payload = {'account': 'shared-claude', 'engine': 'claude', 'profile': None, 'mode': 'interactive'}
    result = na.probe(payload, 'claude', 'shared-claude', 'claude', request=hub)
    assert result['launch_allowed'] is True and result['profile_supported'] is True
    assert seen == [('/api/engines/admission', {'label': 'shared-claude', 'native': True})]
    refused = na.probe(payload, 'claude', 'shared-claude', 'claude',
                       request=lambda *a, **k: {'launch_allowed': False, 'reason': 'quota_exhausted', 'account_label': 'shared-claude'})
    assert refused['launch_allowed'] is False and refused['reason'] == 'quota_exhausted'


def test_collect_subscriptions_never_runs_quse():
    from agent_hub_host import fleet
    ran = []
    settings = {'subscriptions': [
        {'service': 'codex', 'account_label': 'shared-codex', 'plan': 'pro'},
        {'service': 'claude', 'account_label': 'shared-claude', 'plan': 'max', 'quota_argv': ['/home/u/.local/bin/quse', 'claude', '--json']},
    ]}
    try:
        fleet.collect_subscriptions(settings, runner=lambda argv: ran.append(argv) or {})
    except Exception:
        pass
    assert not any('quse' in ' '.join(map(str, a)) for a in ran)
    assert fleet._is_quota_cli(['C:\\venv\\Scripts\\quse.exe', 'codex'])
    assert fleet._is_quota_cli([sys.executable, '-m', 'quse.cli', 'codex'])
    assert not fleet._is_quota_cli(['/opt/adapter', 'codex'])


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, status=200, body=None):
        self.status, self.body, self.requests = status, body, []

    def open(self, req, timeout=None):
        self.requests.append(req)
        if self.status != 200:
            raise urllib.error.HTTPError(req.full_url, self.status, 'x', {}, io.BytesIO(b'{}'))
        return FakeResponse(json.dumps(self.body).encode())


def test_direct_provider_quota_probe(home):
    dest, h = home
    (h / PATH).write_text(auth('a'))
    opener = Opener(body={'rate_limit': {'limit_reached': False, 'primary_window': {'used_percent': 30, 'limit_window_seconds': 604800, 'reset_at': 1893456000}}})
    raw = c.probe_quota('codex', h, opener=opener)
    assert raw['codex']['windows']['7d']['percent_remaining'] == 70
    assert opener.requests[0].get_header('Chatgpt-account-id') == 'acct'
    assert c.probe_quota('codex', h, opener=Opener(status=401)) == {'unauthorized': True}
    assert c.probe_quota('codex', h / 'missing', opener=opener) == {}


def test_server_collector_quotas_skip_accounts_without_a_reading(home, monkeypatch):
    _no_quse(monkeypatch)
    dest, h = home
    p.write_json(p.root() / 'state.json', [{'label': LABEL, 'revision': REV, 'provider': 'codex'}])
    p.write_json(dest / 'bundle.json', bundle(1, auth('a')))
    monkeypatch.setattr(c, 'probe_quota', lambda provider, home: {})
    assert p.quotas() == {'accounts': []}
    monkeypatch.setattr(c, 'probe_quota', lambda provider, home: {'codex': c.codex_quota({'rate_limit': {'primary_window': {'used_percent': 1, 'limit_window_seconds': 604800, 'reset_at': 1893456000}}})})
    out = p.quotas()['accounts']
    assert out[0]['account'] == LABEL and out[0]['quota']['status'] == 'ok'


def test_new_host_sync_installs_every_hub_account_without_login(tmp_path, monkeypatch):
    """One sync after enrollment materializes every Hub-held account (I3)."""
    monkeypatch.setenv('AGENT_HUB_MANAGED_DIR', str(tmp_path / 'fresh-host'))
    installed = []
    monkeypatch.setattr(p, 'install', lambda b, request=None: installed.append(b['label']))
    monkeypatch.setattr(p, 'profiles', lambda bundles, path=None: None)
    from agent_hub_host import engine_login
    monkeypatch.setattr(engine_login, 'cancel_removed', lambda bundles: None)
    monkeypatch.setattr(engine_login, 'tick', lambda *a, **k: None)
    claude = {'label': 'hub-claude', 'provider': 'claude', 'version': '2.1.304', 'revision': 'b' * 8 + '-' + 'b' * 4 + '-' + 'b' * 4 + '-' + 'b' * 4 + '-' + 'b' * 12,
              'files': {'.claude/.credentials.json': '{"claudeAiOauth":{}}'}, 'env': {}, 'args': [],
              'credential_generation': 1, 'credential_paths': ['.claude/.credentials.json']}
    posts = []

    def hub(method, path, payload=None):
        if path == '/api/engines/sync':
            return {'engines': [bundle(2, auth('a')), claude], 'logins': []}
        posts.append((path, payload))
        return {'status': 'installed'}
    (tmp_path / 'fresh-host').mkdir(mode=0o700)
    result = p.sync(hub)
    assert installed == [LABEL, 'hub-claude']
    assert result['profiles'] == ['hub-' + LABEL, 'hub-hub-claude']
    assert [x[0] for x in posts] == ['/api/engines/' + LABEL, '/api/engines/hub-claude']


def test_central_claude_login_cli_never_prints_the_code(monkeypatch, capsys):
    from types import SimpleNamespace
    sent = []

    def fake(method, path, body=None):
        sent.append((method, path, body))
        if path == '/api/engines/credential-logins':
            return {'id': '0' * 8 + '-0000-4000-8000-' + '0' * 12, 'url': 'https://claude.com/cai/oauth/authorize?state=s'}
        return {'status': 'configured', 'label': 'hub-claude', 'credential_generation': 1}
    monkeypatch.setattr(p.bus, '_do_request', fake)
    args = SimpleNamespace(provider='claude', label='hub-claude', version=None, adopt=False)
    result = p.central_login(args, prompt=lambda _: 'secret-code#s')
    assert result['credential_generation'] == 1
    assert sent[1][2] == {'code': 'secret-code#s'}
    out = capsys.readouterr()
    assert 'secret-code' not in out.out + out.err
    assert 'https://claude.com/cai/oauth/authorize' in out.err
