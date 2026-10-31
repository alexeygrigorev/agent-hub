"""Intake bf0863c4 desktop blockers (AC-D1, AC-D5, AC-D6).

- A plain install configures the maintained launcher, so the host advertises
  headless as well as interactive (never launcher_not_configured).
- Managed admission takes Hub's verdict after the local reconcile lock, so a
  long Windows lock wait cannot age the verdict past the 30 s window.
- The served Windows installer never runs native steps under EAP Stop.
"""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import zipfile

import pytest

from agent_hub_host import bootstrap, launcher_update, provisioning as p

REPO = Path(__file__).resolve().parents[2]
SHA = 'a' * 40


@pytest.fixture
def install_root(tmp_path, monkeypatch):
    os.chmod(tmp_path, 0o700)
    root = tmp_path / 'agent-hub'
    root.mkdir(mode=0o700)
    monkeypatch.setattr(bootstrap, 'state_root', lambda install_root: root)
    return root


def fake_release(monkeypatch, layout_sha=SHA):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        z.writestr('agent-quota-launcher-' + layout_sha + '/launcher/__init__.py', '')
        z.writestr('agent-quota-launcher-' + layout_sha + '/launcher/cli.py', 'def main():\n    pass\n')
    raw = buffer.getvalue()
    calls = []

    def download(url, target):
        calls.append(url)
        Path(target).write_bytes(raw)
    monkeypatch.setattr(bootstrap, 'download', download)
    monkeypatch.setattr(bootstrap, 'LAUNCHER_RELEASE', {
        'sha': SHA, 'url': 'https://codeload.github.com/x/y/zip/' + SHA,
        'sha256': hashlib.sha256(raw).hexdigest()})
    return calls


def test_pinned_launcher_release_is_full_sha_and_https():
    from agent_hub_host.bootstrap_pins import LAUNCHER_RELEASE
    assert len(LAUNCHER_RELEASE['sha']) == 40 and len(LAUNCHER_RELEASE['sha256']) == 64
    assert LAUNCHER_RELEASE['url'].startswith('https://codeload.github.com/alexeygrigorev/agent-quota-launcher/zip/')
    assert LAUNCHER_RELEASE['url'].endswith(LAUNCHER_RELEASE['sha'])


def test_launcher_release_is_installed_once_and_reported_current(install_root, monkeypatch):
    calls = fake_release(monkeypatch)
    target = bootstrap.ensure_launcher(install_root)
    assert target == install_root / 'launcher-releases' / SHA
    assert (target / 'launcher/cli.py').is_file()
    assert bootstrap.ensure_launcher(install_root) == target
    assert len(calls) == 1
    # launcher_update recognizes the immutable release: no launcher_not_configured.
    reports = []
    request = lambda method, path, body=None: reports.append(body) if method == 'POST' else {}
    launcher_update.step_once(request, {'launcher': {'root': str(target), 'argv': ['python', '-m', 'launcher']}},
                              run=lambda *a, **k: (_ for _ in ()).throw(OSError('no git')))
    assert reports[-1]['current_sha'] == SHA and reports[-1]['error'] != 'launcher_not_configured'


def test_launcher_checksum_mismatch_installs_nothing(install_root, monkeypatch):
    fake_release(monkeypatch)
    monkeypatch.setitem(bootstrap.LAUNCHER_RELEASE, 'sha256', '0' * 64)
    with pytest.raises(ValueError, match='checksum'):
        bootstrap.ensure_launcher(install_root)
    assert not (install_root / 'launcher-releases' / SHA).exists()


def test_interrupted_release_is_set_aside_not_reused(install_root, monkeypatch):
    fake_release(monkeypatch)
    partial = install_root / 'launcher-releases' / SHA
    (partial / 'launcher').mkdir(parents=True)
    target = bootstrap.ensure_launcher(install_root)
    assert (target / 'launcher/cli.py').is_file()
    assert [e.name.startswith('.incomplete-') for e in (install_root / 'launcher-releases').iterdir()].count(True) == 1


def write_settings(path, **extra):
    data = {'version': 1, 'subscriptions': [], 'capabilities': [], 'max_agents': 1,
            'workspaces': {'home': str(Path.home())}, **extra}
    path.write_text(json.dumps(data))
    return path


def test_plain_install_configures_headless_launcher(install_root, tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'managed_subscriptions', lambda: [])
    settings = write_settings(tmp_path / 'subscriptions.json')
    release = install_root / 'launcher-releases' / SHA
    assert bootstrap.configure_launcher(settings, install_root, release) == 'configured'
    launcher = json.loads(settings.read_text())['launcher']
    assert launcher == {'argv': [sys.executable, '-m', 'launcher'], 'root': str(release),
                        'config_dir': str(install_root / 'launcher-config')}
    assert (install_root / 'launcher-config').is_dir()
    # The host advertises headless exactly when a launcher is configured.
    from agent_hub_host import fleet
    assert fleet.load_settings(settings).get('launcher')


def test_installer_launcher_is_refreshed_on_rerun(install_root, tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'managed_subscriptions', lambda: [])
    old = {'argv': ['C:/old-release/python.exe', '-m', 'launcher'],
           'root': str(install_root / 'launcher-releases' / ('b' * 40)), 'config_dir': 'C:/cfg'}
    settings = write_settings(tmp_path / 'subscriptions.json', launcher=old)
    release = install_root / 'launcher-releases' / SHA
    assert bootstrap.configure_launcher(settings, install_root, release) == 'configured'
    assert json.loads(settings.read_text())['launcher']['root'] == str(release)


def test_operator_launcher_is_never_replaced(install_root, tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'managed_subscriptions', lambda: [])
    operator = {'argv': ['/usr/bin/systemd-run', '--user', '--', 'python3', '-m', 'launcher'],
                'root': str(install_root / 'launcher-releases' / SHA), 'config_dir': '/srv/launcher'}
    settings = write_settings(tmp_path / 'subscriptions.json', launcher=operator)
    before = settings.read_text()
    assert bootstrap.configure_launcher(settings, install_root, install_root / 'launcher-releases' / SHA) == 'operator'
    assert settings.read_text() == before


def test_admission_stamps_hub_verdict_after_local_lock(tmp_path, monkeypatch):
    os.chmod(tmp_path, 0o700)
    m_dir = tmp_path / 'managed-engines'
    m_dir.mkdir(mode=0o700)
    monkeypatch.setenv('APLEXER_CONFIG', str(tmp_path / 'aplexer/config.toml'))
    monkeypatch.setattr(p, 'root', lambda: m_dir)
    b = dict(label='hub-claude', provider='claude', version='2.1.304', args=[], files={}, env={},
             revision='11111111-1111-4111-8111-111111111111')
    dest = m_dir / (b['label'] + '-' + b['revision'])
    dest.mkdir(mode=0o700)
    p.write_json(dest / 'bundle.json', b)
    p.write_json(m_dir / 'state.json', [{'label': b['label'], 'revision': b['revision'], 'provider': 'claude'}])
    events = []

    @contextlib.contextmanager
    def slow_lock(timeout=None):
        assert timeout == p.ADMISSION_LOCK_WAIT_S
        events.append('lock')  # e.g. a Windows reconcile holding it through npm
        yield
    monkeypatch.setattr(p, 'lock', slow_lock)

    def hub(method, path, body):
        events.append('hub')
        return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'hub-claude',
                'observed_at': '2026-10-31T12:00:00.000Z'}
    monkeypatch.setattr(p, 'host_request', hub)
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'profile': 'hub-hub-claude', 'account': 'hub-claude'})))
    monkeypatch.setattr(sys, 'argv', ['admission', 'hub-claude'])
    out = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', out)
    p.admission()
    assert events[:2] == ['lock', 'hub']
    assert json.loads(out.getvalue())['launch_allowed'] is True


def test_windows_installer_never_stops_on_native_stderr():
    script = (REPO / 'public/cli/host-install.ps1').read_text()
    assert "$ErrorActionPreference = 'Stop'" not in script
    assert "$ErrorActionPreference = 'Continue'" in script
    # Every native step is checked by exit code instead.
    assert script.count('$LASTEXITCODE -ne 0') >= 2
    assert '2>&1' in script.split('& $Uv run', 1)[1].splitlines()[0]


def test_admission_refusal_carries_fixed_gate_reason():
    from agent_hub_host import dispatch_diagnostics as d
    diag = d.classify(d.AdmissionRefusedError('Fresh admission refused', 'local_lock_unavailable'))
    assert diag['reason'] == 'admission_refused' and diag['gate_reason'] == 'local_lock_unavailable'
    # Free text never leaves the host.
    assert d.classify(d.AdmissionRefusedError('Fresh admission refused', 'token=abc'))['gate_reason'] == 'gate_refused'
    assert d.classify(d.AdmissionRefusedError('Fresh admission timestamp required', 'admission_timestamp_stale'))['reason'] == 'admission_refused'
    assert 'gate_reason' not in d.classify(ValueError('Fresh admission refused'))


def test_typescript_gate_reasons_match_host():
    from agent_hub_host import dispatch_diagnostics as d
    import re
    ts = (REPO / 'src/fleet.ts').read_text()
    block = ts.split('const GATE_REASONS = new Set([', 1)[1].split('])', 1)[0]
    assert set(re.findall(r"'([a-z_]+)'", block)) == d.GATE_REASONS


def test_admission_lock_failure_is_named(tmp_path, monkeypatch):
    os.chmod(tmp_path, 0o700)
    monkeypatch.setattr(p, 'root', lambda: tmp_path)

    @contextlib.contextmanager
    def broken_lock(timeout=None):
        raise PermissionError('locked')
        yield
    monkeypatch.setattr(p, 'lock', broken_lock)
    monkeypatch.setattr(p, 'host_request', lambda *a: {'launch_allowed': True, 'profile_supported': True,
                                                       'account_label': 'hub-claude', 'observed_at': 'x'})
    monkeypatch.setattr(sys, 'stdin', io.StringIO(''))
    monkeypatch.setattr(sys, 'argv', ['admission', 'hub-claude'])
    out = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', out)
    p.admission()
    assert json.loads(out.getvalue())['reason'] == 'local_lock_unavailable'


def test_admission_reads_atomic_state_when_lock_unavailable(tmp_path, monkeypatch):
    os.chmod(tmp_path, 0o700)
    m_dir = tmp_path / 'managed-engines'
    m_dir.mkdir(mode=0o700)
    monkeypatch.setenv('APLEXER_CONFIG', str(tmp_path / 'aplexer/config.toml'))
    monkeypatch.setattr(p, 'root', lambda: m_dir)
    b = dict(label='hub-claude', provider='claude', version='2.1.304', args=[], files={}, env={},
             revision='11111111-1111-4111-8111-111111111111')
    dest = m_dir / (b['label'] + '-' + b['revision'])
    dest.mkdir(mode=0o700)
    p.write_json(dest / 'bundle.json', b)
    p.write_json(m_dir / 'state.json', [{'label': b['label'], 'revision': b['revision'], 'provider': 'claude'}])

    @contextlib.contextmanager
    def busy_lock(timeout=None):
        raise OSError('msvcrt lock busy')
        yield
    monkeypatch.setattr(p, 'lock', busy_lock)
    monkeypatch.setattr(p, 'host_request', lambda *a: {'launch_allowed': True, 'profile_supported': True,
                                                       'account_label': 'hub-claude', 'observed_at': 'x'})
    monkeypatch.setattr(sys, 'stdin', io.StringIO(''))
    monkeypatch.setattr(sys, 'argv', ['admission', 'hub-claude'])
    out = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', out)
    p.admission()
    assert json.loads(out.getvalue())['launch_allowed'] is True


# Windows package installs self-update by rerunning the public installer.
def make_release(tmp_path, sha):
    release = tmp_path / 'releases' / (sha[:16] + '-' + 'c' * 12)
    release.mkdir(parents=True)
    (release / 'runtime-ready.json').write_text(json.dumps({'archive_sha256': sha}))
    return release


def test_package_update_current_when_served_matches(tmp_path):
    from agent_hub_host import package_update as u
    release = make_release(tmp_path, 'd' * 64)
    registered = []
    state = u.step_once('https://hub.example', tmp_path, release=release, fetch=lambda url: 'd' * 64 + '  hub.pyz\n',
                        register=lambda *a: registered.append(a))
    assert state == 'current' and not registered


def test_package_update_schedules_once_per_served_archive(tmp_path):
    from agent_hub_host import package_update as u
    release = make_release(tmp_path, 'd' * 64)
    registered = []
    fetched = []

    def fetch(url):
        fetched.append(url)
        return 'e' * 64 + '\n'
    run = lambda **kw: u.step_once('https://hub.example', tmp_path, release=release, fetch=fetch,
                                   register=lambda *a: registered.append(a), free_gb=500, **kw)
    assert run(now=1000.0) == 'scheduled'
    assert fetched == ['https://hub.example/cli/hub.pyz.sha256']
    assert run(now=2000.0) == 'pending'
    assert len(registered) == 1
    # Same archive retried only after both the retry and spacing windows.
    assert run(now=1000.0 + u.RETRY_S + 1) == 'pending'
    assert run(now=1000.0 + u.MIN_INTERVAL_S + 1) == 'scheduled'
    assert len(registered) == 2


def test_package_update_needs_disk_room(tmp_path):
    from agent_hub_host import package_update as u
    release = make_release(tmp_path, 'd' * 64)
    assert u.step_once('https://hub.example', tmp_path, release=release, fetch=lambda x: 'e' * 64,
                       register=lambda *a: pytest.fail('must not register'), free_gb=10) == 'disk_low'


def test_package_update_ignores_unknown_release_and_bad_checksum(tmp_path):
    from agent_hub_host import package_update as u
    never = lambda *a: pytest.fail('must not register')
    assert u.step_once('https://hub.example', tmp_path, release=tmp_path / 'nope', fetch=lambda u_: 'e' * 64,
                       register=never) == 'unsupported'
    release = make_release(tmp_path, 'd' * 64)
    assert u.step_once('https://hub.example', tmp_path, release=release, fetch=lambda u_: '<html>',
                       register=never) == 'served_unknown'
    # A marker whose sha does not match the release name is not trusted.
    (release / 'runtime-ready.json').write_text(json.dumps({'archive_sha256': 'f' * 64}))
    assert u.current_archive_sha(release) is None


def test_package_update_task_runs_only_the_public_installer(tmp_path):
    from agent_hub_host import package_update as u
    body = u.task_script('https://hub.dtcdev.click/', tmp_path / "it's.log")
    assert "irm 'https://hub.dtcdev.click/host/install.ps1' | iex" in body
    assert "it''s.log" in body
    with pytest.raises(ValueError):
        u.task_script('http://hub.dtcdev.click', tmp_path / 'x.log')
    with pytest.raises(ValueError):
        u.task_script("https://hub.dtcdev.click'; calc; '", tmp_path / 'x.log')
    calls = []

    class R:
        returncode = 0
    u.register_task('https://hub.dtcdev.click', tmp_path, run=lambda argv, **kw: calls.append(argv) or R())
    assert (tmp_path / 'package-update.ps1').read_text() == u.task_script('https://hub.dtcdev.click', tmp_path / 'package-update.log')
    script = calls[0][-1]
    assert 'AgentHubUpdate-' in script and '-RunLevel Limited' in script and 'Highest' not in script


def test_package_update_is_windows_package_only(monkeypatch):
    from agent_hub_host import package_update as u
    monkeypatch.setattr(u.sys, 'platform', 'linux')
    assert u.maybe_update() is None
    monkeypatch.setattr(u.sys, 'platform', 'win32')
    monkeypatch.setattr(u, 'running_release', lambda: None)
    assert u.maybe_update() is None


def test_launcher_setup_is_optional_and_platform_gated(install_root, tmp_path, monkeypatch):
    monkeypatch.setattr(p, 'managed_subscriptions', lambda: [])
    settings = write_settings(tmp_path / 'subscriptions.json')
    before = settings.read_text()
    monkeypatch.setattr(bootstrap.sys, 'platform', 'darwin')
    assert bootstrap.setup_launcher(settings, install_root) == 'unsupported_platform'
    monkeypatch.setattr(bootstrap.sys, 'platform', 'linux')
    monkeypatch.setattr(bootstrap, 'ensure_launcher', lambda root: (_ for _ in ()).throw(OSError('offline')))
    assert bootstrap.setup_launcher(settings, install_root) == 'unavailable'
    release = install_root / 'launcher-releases' / SHA
    monkeypatch.setattr(bootstrap, 'ensure_launcher', lambda root: release)
    monkeypatch.setattr(bootstrap, 'launcher_smoke', lambda root: False)
    assert bootstrap.setup_launcher(settings, install_root) == 'unavailable'
    assert settings.read_text() == before
    monkeypatch.setattr(bootstrap, 'launcher_smoke', lambda root: True)
    assert bootstrap.setup_launcher(settings, install_root) == 'configured'


def test_launcher_smoke_runs_real_release(install_root, monkeypatch):
    fake_release(monkeypatch)
    target = bootstrap.ensure_launcher(install_root)
    (target / 'launcher/__main__.py').write_text('import sys\nsys.exit(0)\n')
    assert bootstrap.launcher_smoke(target)
    (target / 'launcher/__main__.py').write_text('raise SystemExit(3)\n')
    assert not bootstrap.launcher_smoke(target)


def test_reject_retries_without_gate_reason_on_old_hub():
    import inspect
    from agent_hub_host import fleet
    src = inspect.getsource(fleet)
    assert "if 'gate_reason' not in diagnostic or '400' not in str(reject_exc)" in src
