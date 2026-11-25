"""Targeted release safety checks; no network, live service or live venv writes."""
import ast
from collections.abc import Mapping
import contextlib
from dataclasses import dataclass
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import itertools
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import threading
import unittest
import venv
import zipapp
import zipfile
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), REPO / 'scripts' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


updater = module('hub2-self-update')
builder = module('build-hub2-release')
enrollment = module('hub2-enroll')
SHA = 'a' * 40
OLD_SHA = 'b' * 40
MACHINE = 'release-test-machine'
STATUS_PATH = '/api/fleet/routing/self'
HOST = f'agent_hub_host_v2-2.0.0+g{SHA}-py3-none-any.whl'

# Provenance: cached pre-unification commit 0cc47732c0d496973f49abf53bbe21458f5f2787,
# scripts/hub2-self-update.py lines 902-922
# (full file sha256: 21850e1c353f72611a66765383498f4c55c144b21d9b84111de8dd8225c287dc,
#  snippet sha256:   826e90bb8174e3bf1c8894535cc1b7fcdf93799cdb03196200617393a702e3b4).
# This is cached pre-unification source evidence of the real failure mechanism,
# not a claim these exact bytes were installed on a specific historical host.
LEGACY_PREPARE_ENVIRONMENT_SOURCE = """\
def prepare_environment(target, download, manifest, host_wheel):
    python = target / 'bin/python'
    uv = shutil.which('uv')
    if uv:
        command([uv, 'venv', '--allow-existing', '--python', sys.executable, target])
        installer = [uv, 'pip', 'install', '--python', python]
    else:
        command([sys.executable, '-m', 'venv', target])
        installer = [python, '-m', 'pip', 'install']
    command([*installer, '--no-index', '--no-deps',
             download / 'websockets-16.0-py3-none-any.whl', download / host_wheel])
    check_host_package(target, manifest)
    command([target / 'bin/hub2', '--help'])
    shutil.copyfile(download / 'hub.pyz', target / 'hub.pyz')
    shutil.copyfile(download / 'hub2-enroll.py', target / 'hub2-enroll.py')
    if ALERT_CLIENT in manifest['artifacts']:
        retain_alert_client(target, manifest, (download / ALERT_CLIENT).read_bytes())
    (target / 'hub.pyz').chmod(0o755)
    command([python, target / 'hub.pyz', '--help'])
    install_operator_cli(target, manifest)
    write_json(target / 'release.json', manifest)
"""


@dataclass(frozen=True)
class _RunSpawnPermit:
    token: int
    thread_id: int
    argv: tuple[str, ...]
    popen_kwargs: tuple[tuple[str, object], ...]


class HermeticProcessFixture:
    """Fixture-lifetime default deny for process effects.

    Any updater.command, subprocess.run or subprocess.Popen call that the
    running test has not explicitly allowed fails closed instead of reaching
    the real host (incident cli681-c6-runtime-incident-v1: unmocked updater
    runtime restarted the live service). A test that genuinely needs a child
    registers its complete expected argv/cwd/env/kwargs contract through
    owned_child(); scoped synthetic patches such as install()'s status
    command mock stack on top of this guard and never widen it. Children run
    under owned_child_env(): import (PYTHON*), Hub/credential (HUB*) and
    aplexer/profile (APLEXER*) selectors from the worker environment are
    removed and HOME points at the owned temporary, so an owned child can
    import only the fixture packages it was given, never worker source,
    credentials, Hub state or profiles.
    """

    # Worker-environment selector prefixes that must never leak into an
    # owned child: import injection, Hub/credential routing and aplexer/
    # profile worker state. Exceeds the production REPIN_ENV_DROP vocabulary.
    AMBIENT_CHILD_ENV_DROP_PREFIXES = ('PYTHON', 'HUB', 'APLEXER')

    def install_process_guard(self):
        self._child_allowances = []
        self._run_spawn_permits = []
        self._run_permit_lock = threading.Lock()
        self._run_permit_tokens = itertools.count(1)
        self._real_subprocess_run = subprocess.run
        self._real_subprocess_popen = subprocess.Popen
        deny_command = patch.object(updater, 'command', self._deny_command)
        deny_command.start()
        self.addCleanup(deny_command.stop)
        guarded_run = patch.object(subprocess, 'run', self._guarded_run)
        guarded_run.start()
        self.addCleanup(guarded_run.stop)
        guarded_popen = patch.object(subprocess, 'Popen', self._guarded_popen)
        guarded_popen.start()
        self.addCleanup(guarded_popen.stop)

    def _deny_command(self, args, *rest, **kwargs):
        raise AssertionError('release test reached updater.command outside an explicit allowance: '
                             + repr([str(value) for value in args])[:160])

    @classmethod
    def _freeze_contract_value(cls, value):
        if value is None or isinstance(value, (bool, int, float, str, bytes)):
            return value
        if isinstance(value, os.PathLike):
            return os.fspath(value)
        if isinstance(value, Mapping):
            return ('dict', tuple(sorted(
                (cls._freeze_contract_value(k), cls._freeze_contract_value(v))
                for k, v in value.items()
            )))
        if isinstance(value, (list, tuple)):
            return ('seq', tuple(cls._freeze_contract_value(item) for item in value))
        if isinstance(value, (set, frozenset)):
            items = [cls._freeze_contract_value(item) for item in value]
            return ('set', tuple(sorted(((repr(item), item) for item in items), key=lambda pair: pair[0])))
        return ('obj', value)

    @classmethod
    def _freeze_popen_kwargs(cls, kwargs):
        return tuple(sorted(
            (key, cls._freeze_contract_value(value))
            for key, value in kwargs.items()
        ))

    @staticmethod
    def _expected_popen_kwargs(run_kwargs):
        popen_kwargs = dict(run_kwargs)
        run_input = popen_kwargs.pop('input', None)
        capture_output = popen_kwargs.pop('capture_output', False)
        popen_kwargs.pop('timeout', None)
        popen_kwargs.pop('check', None)
        if run_input is not None:
            if popen_kwargs.get('stdin') is not None:
                raise ValueError('stdin and input arguments may not both be used.')
            popen_kwargs['stdin'] = subprocess.PIPE
        if capture_output:
            if popen_kwargs.get('stdout') is not None or popen_kwargs.get('stderr') is not None:
                raise ValueError('stdout and stderr arguments may not be used with capture_output.')
            popen_kwargs['stdout'] = subprocess.PIPE
            popen_kwargs['stderr'] = subprocess.PIPE
        return popen_kwargs

    def _check_allowance(self, argv, kwargs):
        argv = [value if isinstance(value, str) else str(value) for value in argv]
        if not any(matcher(argv, kwargs) for matcher in self._child_allowances):
            raise AssertionError('release test reached an unallowed real process: ' + repr(argv)[:160])
        return argv

    def _guarded_run(self, argv, *args, **kwargs):
        if args:
            raise AssertionError('guarded subprocess.run permits no positional arguments beyond the command; '
                                 'bind every option as a keyword argument: ' + repr(args)[:80])
        norm_argv = self._check_allowance(argv, kwargs)
        expected_popen_kwargs = self._expected_popen_kwargs(kwargs)
        # The saved real run resolves subprocess.Popen from the patched module
        # globals, so the Popen guard stays installed for the whole blocking
        # call and re-enters for this one child. The validated call receives
        # an exact one-shot permit bound to its owning thread, unique call
        # token, and immutable normalized argv + Popen kwargs/env/cwd snapshot;
        # the guarded Popen consumes it atomically before any wait, and every
        # mismatch or cross-thread attempt fails closed without spending it.
        with self._run_permit_lock:
            permit = _RunSpawnPermit(
                token=next(self._run_permit_tokens),
                thread_id=threading.get_ident(),
                argv=tuple(norm_argv),
                popen_kwargs=self._freeze_popen_kwargs(expected_popen_kwargs),
            )
            self._run_spawn_permits.append(permit)
        try:
            return self._real_subprocess_run(list(norm_argv), **kwargs)
        finally:
            with self._run_permit_lock:
                if permit in self._run_spawn_permits:
                    self._run_spawn_permits.remove(permit)

    def _guarded_popen(self, argv, *args, **kwargs):
        if args:
            raise AssertionError('guarded subprocess.Popen permits no positional arguments beyond the command; '
                                 'bind bufsize/executable etc. as keyword arguments: ' + repr(args)[:80])
        norm_argv = [value if isinstance(value, str) else str(value) for value in argv]
        argv_tuple = tuple(norm_argv)
        frozen_kwargs = self._freeze_popen_kwargs(kwargs)
        caller_thread = threading.get_ident()
        with self._run_permit_lock:
            if self._run_spawn_permits:
                match_index = next(
                    (idx for idx, permit in enumerate(self._run_spawn_permits)
                     if permit.thread_id == caller_thread
                     and permit.argv == argv_tuple
                     and permit.popen_kwargs == frozen_kwargs),
                    None,
                )
                if match_index is None:
                    raise AssertionError(
                        'release test reached a real process other than the pending validated '
                        'run child contract: ' + repr(norm_argv)[:160]
                    )
                self._run_spawn_permits.pop(match_index)
                consumed_permit = True
            else:
                consumed_permit = False
        if consumed_permit:
            return self._real_subprocess_popen(norm_argv, **kwargs)
        norm_argv = self._check_allowance(norm_argv, kwargs)
        return self._real_subprocess_popen(norm_argv, **kwargs)

    @contextlib.contextmanager
    def owned_child(self, matcher):
        """The one explicit fixture-scoped real-child allowance: while the
        context is open, the guard executes only the complete argv/cwd/env/
        kwargs contract the matcher validates, and nothing else."""
        self._child_allowances.append(matcher)
        try:
            yield
        finally:
            self._child_allowances.remove(matcher)

    def owned_child_env(self, **overrides):
        """Sanitized environment for a fixture-owned real child.

        Import (PYTHON*), Hub/credential (HUB*) and aplexer/profile
        (APLEXER*) worker selectors are removed, HOME is bound to the owned
        temporary, and overrides re-add exactly what one synthetic test
        needs. The whole returned mapping is what every allowance must bind
        exactly."""
        sanitized = {key: value for key, value in os.environ.items()
                     if not key.startswith(self.AMBIENT_CHILD_ENV_DROP_PREFIXES)}
        sanitized['HOME'] = str(Path(self.temporary.name))
        sanitized.update(overrides)
        return sanitized

    def install_hermetic_tool_discovery(self):
        """Deterministic hermetic provisioning-tool discovery for release
        updater lifecycles (CI run 44481800454 at bd7761f74: GitHub runners
        ship a real uv on PATH; the generic install fixtures discovered
        /opt/hostedtoolcache/Python/3.12.16/x64/bin/uv and the owned-command
        guard correctly failed closed on that external binary, while hosts
        without ambient uv silently took the stdlib fallback — an
        ambient-dependent fixture contract). Under this scope the updater
        never resolves uv from ambient PATH, so every provisioning lifecycle
        takes the deterministic offline stdlib venv/pip fallback; a test
        that genuinely studies the production uv branch binds its own owned
        mock with a nested patch, which wins for the duration of its
        install() call. Provider PATH discovery (PROVIDER_PATH_COMMANDS,
        including its explicit path= forms) keeps the real shutil.which."""
        real_which = shutil.which

        def scoped(name, *rest, **kwargs):
            if name == 'uv':
                return None
            return real_which(name, *rest, **kwargs)

        discovery = patch.object(updater.shutil, 'which', scoped)
        discovery.start()
        self.addCleanup(discovery.stop)


class ReleaseTests(HermeticProcessFixture, unittest.TestCase):
    def test_native_self_update_bypasses_config_and_propagates_exit(self):
        code = '''
from types import SimpleNamespace
from unittest.mock import patch
from agent_hub_host.cli import main
with patch('subprocess.run', return_value=SimpleNamespace(returncode=7)) as run:
    assert main(['--config','/missing/config','self-update','--root','/isolated/root','--no-restart']) == 7
    args = run.call_args.args[0]
    assert args[1] == '/isolated/root/updater.py' and '--no-restart' in args
'''
        subprocess_argv = [sys.executable, '-c', code]
        child_env = self.owned_child_env(PYTHONPATH=str(REPO / 'hub2/host'))
        with self.owned_child(lambda argv, kwargs: argv == subprocess_argv
                              and kwargs.get('check') is True and kwargs.get('cwd') is None
                              and kwargs.get('env') == child_env
                              and sorted(kwargs) == ['check', 'env', 'stderr', 'stdout']):
            subprocess.run(subprocess_argv, check=True, env=child_env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def setUp(self):
        (REPO / '.local/tmp').mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=REPO / '.local/tmp', prefix='test-release-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'host'
        self.files = {name: ('artifact:' + name).encode() for name in
                      (HOST, 'websockets-16.0-py3-none-any.whl', 'hub.pyz',
                       'hub2-self-update.py', 'hub2-enroll.py', 'host-install.sh', *updater.UNITS)}
        self.files['manifest.json'] = json.dumps({'built_at_ms': 1000}).encode()
        self.manifest = {'schema_version': 1, 'protocol_version': 1, 'host_version': SHA,
                         'python_requires': '>=3.11',
                         'artifacts': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
                                       for name, data in self.files.items()}}
        self.calls = []
        self.downloads = []
        self.config = Path(self.temporary.name) / '.config/agent-hub/v2.json'
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps({'machine_id': MACHINE, 'token': 'synthetic-enrollment-token'}))
        self.status = {'machine_id': MACHINE, 'connected': True, 'host_version': SHA,
                       'protocol_version': 1, 'version_mismatch': False}
        self.status_rc = 0
        self.status_output = None
        self.status_timeout = False
        # Hermetic profile/config selectors: the updater honors absolute
        # ambient selectors (APLEXER_CONFIG, XDG_CONFIG_HOME), so every test
        # binds HOME and all of them to this disposable owned root before any
        # install/recovery/repin path runs. Real profile files are never read.
        owned = Path(self.temporary.name)
        selectors = {'HOME': str(owned), 'USERPROFILE': str(owned),
                     'XDG_CONFIG_HOME': str(owned / '.config'),
                     'APPDATA': str(owned / 'AppData' / 'Roaming'),
                     'APLEXER_CONFIG': str(owned / '.config' / 'aplexer' / 'config.toml')}
        profiles = patch.dict(os.environ, selectors)
        profiles.start()
        self.addCleanup(profiles.stop)
        # Default deny holds for every test in this class, not only for the
        # ones that remember to patch: an omitted per-test mock cannot reach
        # the real service tool or spawn an unregistered process.
        self.install_process_guard()
        # Hermetic provisioning discovery for every updater lifecycle in this
        # class (install(), prepare_environment(), install_operator_cli()):
        # ambient PATH must never decide which provisioning branch runs.
        # HostDropinAlignmentTests shares this fixture but has no
        # provisioning lifecycle, so it does not take this scope.
        self.install_hermetic_tool_discovery()

    def fetch(self, url, limit):
        name = url.rsplit('/', 1)[1]
        self.downloads.append(name)
        return json.dumps(self.manifest).encode() if name == 'host-release.json' else self.files[name]

    def _owned_command_executable(self, executable):
        # Fail closed on any external command: only the mocked service tool,
        # the standard venv/uv provisioning tools and binaries inside the
        # owned temporary root are accepted.
        if executable in ('systemctl', 'uv'):
            return True
        path = Path(executable)
        if path == Path(sys.executable):
            return True
        return (path.is_absolute()
                and os.path.commonpath((str(path), self.temporary.name)) == self.temporary.name)

    def command(self, args):
        args = [str(value) for value in args]
        if not self._owned_command_executable(args[0]):
            raise AssertionError('install test reached an external command: ' + args[0])
        self.calls.append(args)

    def status_command(self, args, *, stdout, stderr, timeout):
        self.calls.append(args)
        self.assertEqual(args, [str(self.root / 'releases' / SHA / 'bin/python'), '-I', '-m', 'agent_hub_host.cli', '--config', str(self.config),
                                'request', 'GET', STATUS_PATH])
        self.assertEqual(stdout, subprocess.PIPE)
        self.assertEqual(stderr, subprocess.DEVNULL)
        self.assertEqual(timeout, 15)
        if self.status_timeout:
            raise subprocess.TimeoutExpired(args, timeout, output=b'synthetic-sensitive-output')
        raw = json.dumps(self.status).encode() if self.status_output is None else self.status_output
        return subprocess.CompletedProcess(args, self.status_rc, stdout=raw)

    def old_release(self):
        old = self.root / 'releases' / OLD_SHA
        old.mkdir(parents=True)
        (self.root / 'current').symlink_to(old)
        return old

    def install(self, **kwargs):
        with patch.object(updater, 'fetch', self.fetch), patch.object(updater, 'command', self.command), \
             patch.object(updater.time, 'sleep'), patch.object(Path, 'home', return_value=Path(self.temporary.name)), \
             patch.object(updater.subprocess, 'run', self.status_command):
            updater.install('https://example.invalid', self.root, **kwargs)

    def policy(self, last=1, pending=None):
        self.root.mkdir(exist_ok=True)
        updater.write_json(self.root / updater.ACTIVATION_RECEIPT,
                           {'last_activation': last, 'pending_since': pending})

    def receipt(self):
        return json.loads((self.root / updater.ACTIVATION_RECEIPT).read_text())

    def add_alert_client(self):
        profiles = patch.dict(os.environ, {'APLEXER_CONFIG': str(Path(self.temporary.name) / 'absent-profiles')})
        profiles.start()
        self.addCleanup(profiles.stop)
        data = b'# selected standalone client\n'
        self.files['hub_alert_client.py'] = data
        self.manifest['artifacts']['hub_alert_client.py'] = {
            'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
        return data

    def test_alert_client_retained_and_verified_before_receipt(self):
        data = self.add_alert_client()
        original = updater.write_json
        seen = []
        def write(path, value):
            if path.name == 'release.json':
                client = path.parent / 'hub_alert_client.py'
                self.assertEqual(client.read_bytes(), data)
                self.assertEqual(client.stat().st_mode & 0o777, 0o600)
                seen.append(path)
            return original(path, value)
        with patch.object(updater, 'write_json', write):
            self.install(restart=False)
        self.assertEqual(len(seen), 1)
        target = (self.root / 'current').resolve()
        updater.verify_alert_client(target, self.manifest)
        self.assertEqual(json.loads((target / 'release.json').read_text()), self.manifest)

    def test_alert_client_bad_download_never_selects_candidate(self):
        self.add_alert_client()
        old = self.old_release()
        self.files['hub_alert_client.py'] = b'wrong payload'
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.install(restart=False)
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertFalse((self.root / 'releases' / SHA / 'release.json').exists())

    def test_alert_client_prepare_rejects_missing_or_tampered_staging(self):
        self.add_alert_client()
        for data in (None, b'bad'):
            with self.subTest(data=data):
                target = Path(self.temporary.name) / ('candidate-' + str(data is None))
                download = target / 'download'
                download.mkdir(parents=True)
                for name in ('hub.pyz', 'hub2-enroll.py'):
                    (download / name).write_bytes(self.files[name])
                if data is not None:
                    (download / 'hub_alert_client.py').write_bytes(data)
                with patch.object(updater, 'command', self.command), \
                     self.assertRaises((FileNotFoundError, ValueError)):
                    updater.prepare_environment(target, download, self.manifest, HOST)
                self.assertFalse((target / 'release.json').exists())

    def test_alert_client_same_version_repairs_exact_manifest_without_restart(self):
        data = self.add_alert_client()
        self.install(restart=False)
        target = (self.root / 'current').resolve()
        receipt = (target / 'release.json').read_bytes()
        client = target / 'hub_alert_client.py'
        for damage in ('missing', 'tampered'):
            with self.subTest(damage=damage):
                if damage == 'missing':
                    client.unlink()
                else:
                    client.write_bytes(b'x' * len(data))
                self.calls.clear(); self.downloads.clear()
                self.install(restart=False)
                self.assertEqual(client.read_bytes(), data)
                self.assertEqual(self.downloads, ['host-release.json', 'hub_alert_client.py'])
                self.assertEqual(self.calls, [])
                self.assertEqual((target / 'release.json').read_bytes(), receipt)
                self.assertEqual((self.root / 'current').resolve(), target)

    def test_alert_client_same_version_rebuild_cannot_change_binding(self):
        self.add_alert_client()
        self.install(restart=False)
        target = (self.root / 'current').resolve()
        client = target / 'hub_alert_client.py'
        original = client.read_bytes()
        client.unlink()
        self.files['hub_alert_client.py'] = b'changed same-version client'
        self.manifest['artifacts']['hub_alert_client.py'] = {
            'sha256': hashlib.sha256(self.files['hub_alert_client.py']).hexdigest(),
            'size': len(self.files['hub_alert_client.py'])}
        self.downloads.clear(); self.calls.clear()
        with self.assertRaisesRegex(ValueError, 'same-version companion artifact mismatch'):
            self.install(restart=False)
        self.assertEqual(self.downloads, ['host-release.json'])
        self.assertEqual(self.calls, [])
        self.assertFalse(client.exists())
        self.assertEqual((self.root / 'current').resolve(), target)
        self.assertNotEqual(original, self.files['hub_alert_client.py'])

    def test_alert_client_bad_repair_preserves_current_and_receipt(self):
        self.add_alert_client()
        self.install(restart=False)
        target = (self.root / 'current').resolve()
        client = target / 'hub_alert_client.py'
        receipt = (target / 'release.json').read_bytes()
        client.unlink()
        self.files['hub_alert_client.py'] = b'corrupt repair'
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.install(restart=False)
        self.assertFalse(client.exists())
        self.assertEqual((target / 'release.json').read_bytes(), receipt)
        self.assertEqual((self.root / 'current').resolve(), target)

    def test_alert_client_readiness_rejects_missing_tampered_and_symlink(self):
        data = self.add_alert_client()
        self.install(restart=False)
        target = (self.root / 'current').resolve()
        client = target / 'hub_alert_client.py'
        for damage in ('missing', 'tampered', 'symlink'):
            with self.subTest(damage=damage):
                client.unlink(missing_ok=True)
                if damage == 'tampered':
                    client.write_bytes(b'x' * len(data))
                elif damage == 'symlink':
                    foreign = Path(self.temporary.name) / 'foreign-client'
                    foreign.write_bytes(data)
                    client.symlink_to(foreign)
                with self.assertRaisesRegex(ValueError, 'artifact mismatch|unmanaged path'):
                    updater.check_service_ready(target)
        self.downloads.clear()
        with self.assertRaisesRegex(ValueError, 'unmanaged path'):
            self.install(restart=False)
        self.assertEqual(self.downloads, ['host-release.json'])
        self.assertTrue(client.is_symlink())

    def test_alert_client_retained_reactivation_rebuilds_companion(self):
        data = self.add_alert_client()
        current, retained, _ = self.prepare_retained_switch()
        (retained / 'hub_alert_client.py').write_bytes(b'untrusted retained copy')
        self.install()
        self.assertEqual((self.root / 'current').resolve(), retained)
        self.assertEqual((self.root / 'previous').resolve(), current)
        self.assertEqual((retained / 'hub_alert_client.py').read_bytes(), data)
        self.assertIn('hub_alert_client.py', self.downloads)

    def test_alert_client_retained_bad_download_keeps_old_selection(self):
        self.add_alert_client()
        current, retained, _ = self.prepare_retained_switch()
        original = (retained / 'hub_alert_client.py').read_bytes()
        self.files['hub_alert_client.py'] = b'corrupt retained download'
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.install()
        self.assertEqual((self.root / 'current').resolve(), current)
        self.assertEqual((retained / 'hub_alert_client.py').read_bytes(), original)
        self.assertFalse((self.root / 'retained-switch.json').exists())

    def test_alert_client_legacy_manifest_keeps_original_compatibility(self):
        self.add_alert_client()
        self.files.pop('hub_alert_client.py')
        self.manifest['artifacts'].pop('hub_alert_client.py')
        self.install(restart=False)
        target = (self.root / 'current').resolve()
        self.assertFalse((target / 'hub_alert_client.py').exists())
        updater.verify_alert_client(target, self.manifest)
        self.calls.clear(); self.downloads.clear()
        self.install(restart=False)
        self.assertEqual(self.downloads, ['host-release.json'])
        self.assertEqual(self.calls, [])

    def test_scheduled_batch_defers_without_downloading_or_restarting(self):
        old = self.old_release()
        self.policy(last=1000)
        with patch.object(updater.time, 'time', return_value=1001):
            self.install(scheduled=True)
        self.assertEqual(self.downloads, ['host-release.json'])
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertFalse(any('restart' in args for args in self.calls))
        self.assertEqual(self.receipt()['reason'], 'release_batch_interval')
        self.assertEqual(self.receipt()['next_check_at'], 1000 + updater.ACTIVATION_INTERVAL)
        self.assertEqual((self.root / updater.ACTIVATION_RECEIPT).stat().st_mode & 0o777, 0o600)

    def test_busy_deferral_preserves_deadline_across_newer_releases(self):
        old = self.old_release()
        self.policy()
        start = updater.ACTIVATION_INTERVAL + 1
        with patch.object(updater, 'managed_session_count', return_value=3), \
             patch.object(updater.time, 'time', return_value=start):
            self.install(scheduled=True)
        self.assertEqual(self.receipt()['reason'], 'managed_sessions_running')
        self.assertEqual(self.receipt()['managed_sessions'], 3)
        with patch.object(updater, 'managed_session_count', return_value=2), \
             patch.object(updater.time, 'time', return_value=start + 100):
            self.assertTrue(updater.defer_activation(self.root, self.root / 'current', 'c' * 40))
        self.assertEqual(self.receipt()['pending_since'], start)
        self.assertEqual(self.receipt()['version'], 'c' * 40)
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertEqual(self.downloads, ['host-release.json'])

    def test_busy_wait_expires_and_success_starts_new_batch_interval(self):
        self.old_release()
        self.policy(pending=updater.ACTIVATION_INTERVAL + 1)
        now = updater.ACTIVATION_INTERVAL + 1 + updater.MAX_BUSY_WAIT
        reasons = []
        original = updater.write_json
        def capture(path, value):
            if path.name == updater.ACTIVATION_RECEIPT:
                reasons.append(value['reason'])
            return original(path, value)
        with patch.object(updater, 'managed_session_count', return_value=4), \
             patch.object(updater.time, 'time', return_value=now), \
             patch.object(updater, 'write_json', side_effect=capture):
            self.install(scheduled=True)
        self.assertIn('bounded_wait_expired', reasons)
        self.assertEqual(self.receipt()['last_activation'], now)
        self.assertIsNone(self.receipt()['pending_since'])
        self.assertEqual(sum('restart' in args for args in self.calls), 1)
        with patch.object(updater.time, 'time', return_value=now + 1):
            self.assertTrue(updater.defer_activation(self.root, self.root / 'current', 'd' * 40))

    def test_idle_host_activates_without_wait_after_batch_interval(self):
        self.old_release()
        self.policy()
        with patch.object(updater, 'managed_session_count', return_value=0), \
             patch.object(updater.time, 'time', return_value=updater.ACTIVATION_INTERVAL + 1):
            self.install(scheduled=True)
        self.assertEqual(self.receipt()['decision'], 'activated')
        self.assertEqual(sum('restart' in args for args in self.calls), 1)

    def test_session_starting_during_preparation_defers_before_switch(self):
        old = self.old_release()
        self.policy()
        with patch.object(updater, 'managed_session_count', side_effect=[0, 1]), \
             patch.object(updater.time, 'time', return_value=updater.ACTIVATION_INTERVAL + 1):
            self.install(scheduled=True)
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertFalse((self.root / 'releases' / SHA).exists())
        self.assertFalse(any('restart' in args for args in self.calls))
        self.assertEqual(self.receipt()['reason'], 'managed_sessions_running')

    def test_unknown_sessions_defer_but_do_not_block_forever(self):
        self.old_release()
        self.policy()
        start = updater.ACTIVATION_INTERVAL + 1
        with patch.object(updater, 'managed_session_count', return_value=None), \
             patch.object(updater.time, 'time', return_value=start):
            self.assertTrue(updater.defer_activation(self.root, self.root / 'current', SHA))
        self.assertEqual(self.receipt()['reason'], 'managed_sessions_unknown')
        with patch.object(updater, 'managed_session_count', return_value=None), \
             patch.object(updater.time, 'time', return_value=start + updater.MAX_BUSY_WAIT):
            self.assertFalse(updater.defer_activation(self.root, self.root / 'current', SHA))
        self.assertEqual(self.receipt()['reason'], 'bounded_wait_expired')

    def test_first_policy_uses_current_link_activation_time(self):
        self.old_release()
        activated = (self.root / 'current').lstat().st_mtime
        with patch.object(updater.time, 'time', return_value=activated + 1):
            self.assertTrue(updater.defer_activation(self.root, self.root / 'current', SHA))
        self.assertEqual(self.receipt()['last_activation'], activated)

    def test_managed_session_journal_is_read_only_and_conservative(self):
        import sqlite3
        journal = self.config.parent / 'v2-journal.sqlite'
        with patch.object(Path, 'home', return_value=Path(self.temporary.name)):
            self.assertIsNone(updater.managed_session_count())
            self.assertFalse(journal.exists())
            with sqlite3.connect(journal) as db:
                db.execute('CREATE TABLE launches(state TEXT)')
                states = ['running', 'launching', 'pending_admission', 'aborting',
                          'deadline_stopping', 'launch_failed_unconfirmed', 'exited', 'stopped_before_dispatch',
                          'launch_failed_reported']
                db.executemany('INSERT INTO launches VALUES (?)', [(state,) for state in states])
            self.assertEqual(updater.managed_session_count(), 6)
            with sqlite3.connect(journal) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM launches').fetchone()[0], 9)

    def test_invalid_policy_fails_before_activation(self):
        self.old_release()
        self.policy(last=float('nan'))
        with self.assertRaisesRegex(ValueError, 'policy timestamp'):
            self.install(scheduled=True)
        self.assertEqual(self.downloads, ['host-release.json'])

    def test_main_marks_scheduled_only_when_triggered_by_timer(self):
        seen = []
        def fake_install(base_url, root, *, install_service=False, restart=True, scheduled=False):
            seen.append((install_service, restart, scheduled))
        with patch.object(updater, 'install', fake_install), \
             patch.object(Path, 'home', return_value=Path(self.temporary.name)):
            with patch.object(sys, 'argv', ['hub2-self-update.py']), \
                 patch.dict(os.environ, {'TRIGGER_UNIT': 'agent-hub2-host-update.timer'}, clear=False):
                self.assertEqual(updater.main(), 0)
            with patch.object(sys, 'argv', ['hub2-self-update.py']), \
                 patch.dict(os.environ, {}, clear=False):
                os.environ.pop('TRIGGER_UNIT', None)
                self.assertEqual(updater.main(), 0)
        self.assertEqual(seen, [(False, True, True), (False, True, False)])

    def test_manual_start_with_stale_systemd_timer_trigger_is_immediate(self):
        # Intake bf07f437: systemd 255 keeps the previous timer activation's
        # TRIGGER_UNIT/TRIGGER_TIMER_REALTIME_USEC on a manual `systemctl start`.
        now = 1_794_578_913.5
        timer = {'TRIGGER_UNIT': 'agent-hub2-host-update.timer'}
        fresh = {**timer, 'TRIGGER_TIMER_REALTIME_USEC': str(int((now - 0.2) * 1_000_000))}
        stale = {**timer, 'TRIGGER_TIMER_REALTIME_USEC': str(int((now - 156) * 1_000_000))}
        future = {**timer, 'TRIGGER_TIMER_REALTIME_USEC': str(int((now + 60) * 1_000_000))}
        self.assertTrue(updater.timer_triggered(fresh, now))
        self.assertFalse(updater.timer_triggered(stale, now), 'stale trigger means a manual (deploy canary) start')
        self.assertFalse(updater.timer_triggered(future, now))
        self.assertTrue(updater.timer_triggered(timer, now), 'without a trigger time keep the batching default')
        self.assertFalse(updater.timer_triggered({}, now))
        self.assertFalse(updater.timer_triggered({'TRIGGER_UNIT': 'x.path', 'TRIGGER_TIMER_REALTIME_USEC': fresh['TRIGGER_TIMER_REALTIME_USEC']}, now))
        seen = []
        def fake_install(base_url, root, *, install_service=False, restart=True, scheduled=False):
            seen.append(scheduled)
        with patch.object(updater, 'install', fake_install), \
             patch.object(Path, 'home', return_value=Path(self.temporary.name)), \
             patch.object(sys, 'argv', ['hub2-self-update.py']), \
             patch.object(updater.time, 'time', return_value=now):
            for env in (stale, fresh):
                with patch.dict(os.environ, env, clear=False):
                    self.assertEqual(updater.main(), 0)
        self.assertEqual(seen, [False, True])

    def test_valid_update_is_offline_and_keeps_previous(self):
        old = self.old_release()
        self.install()
        self.assertEqual((self.root / 'previous').resolve(), old)
        self.assertEqual((self.root / 'current').resolve().name, SHA)
        self.assertTrue(old.exists())
        pip = next(args for args in self.calls if 'pip' in args)
        self.assertIn('--no-index', pip)
        self.assertIn('--no-deps', pip)
        self.assertFalse(any('systemctl' in args and 'stop' in args for args in self.calls))
        self.assertEqual(os.readlink(self.root / 'current/bin/hub'), '../hub.pyz')

    def test_permissive_umask_install_and_same_version_reuse_keep_private_receipt(self):
        previous = os.umask(0o002)
        try:
            self.install(install_service=True, restart=False)
            target = (self.root / 'current').resolve()
            receipt = target / 'release.json'
            snapshot = target / 'units-before.json'
            original, inode = receipt.read_bytes(), receipt.stat().st_ino
            self.assertEqual(json.loads(original), self.manifest)
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
            self.calls.clear()
            self.install(restart=False)
            self.assertEqual((receipt.read_bytes(), receipt.stat().st_ino), (original, inode))
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.calls, [])
        finally:
            os.umask(previous)

    def test_same_version_does_not_implicitly_mutate_legacy_receipt(self):
        self.install(restart=False)
        receipt = (self.root / 'current').resolve() / 'release.json'
        receipt.chmod(0o664)  # Owned synthetic legacy receipt; no live release.
        original, inode = receipt.read_bytes(), receipt.stat().st_ino
        self.calls.clear()
        self.install(restart=False)
        self.assertEqual((receipt.read_bytes(), receipt.stat().st_ino), (original, inode))
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o664)
        self.assertEqual(self.calls, [])

    def test_private_json_write_does_not_follow_old_fixed_temporary_links(self):
        previous = os.umask(0o002)
        try:
            self.root.mkdir()
            for kind in ('symlink', 'hardlink'):
                with self.subTest(kind=kind):
                    foreign = self.root / (kind + '-foreign')
                    foreign.write_bytes(b'synthetic unrelated bytes')
                    receipt = self.root / (kind + '.json')
                    legacy_temp = receipt.with_name(receipt.name + '.tmp')
                    if kind == 'symlink':
                        legacy_temp.symlink_to(foreign)
                    else:
                        os.link(foreign, legacy_temp)
                    original = foreign.read_bytes()
                    updater.write_json(receipt, {'synthetic': True})
                    self.assertEqual(json.loads(receipt.read_bytes()), {'synthetic': True})
                    self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(receipt.stat().st_nlink, 1)
                    self.assertEqual(foreign.read_bytes(), original)
                    self.assertTrue(legacy_temp.exists())
        finally:
            os.umask(previous)

    def test_retained_rebuild_and_cleanup_resume_preserve_private_receipt(self):
        previous = os.umask(0o002)
        try:
            _, retained, _ = self.prepare_retained_switch()
            receipt = retained / 'release.json'
            original = receipt.read_bytes()
            receipt.chmod(0o664)  # Rebuild verifies and replaces this owned fixture.
            with patch.object(updater, 'finish_retained_switch', side_effect=OSError('cleanup interrupted')):
                with self.assertRaisesRegex(OSError, 'cleanup interrupted'):
                    self.install()
            inode = receipt.stat().st_ino
            self.assertEqual((receipt.read_bytes(), receipt.stat().st_mode & 0o777), (original, 0o600))
            self.calls.clear()
            self.install()
            self.assertEqual((receipt.read_bytes(), receipt.stat().st_ino), (original, inode))
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertFalse((self.root / 'retained-switch-committed.json').exists())
            self.assertEqual(self.calls, [])
        finally:
            os.umask(previous)

    def test_same_version_repairs_wheel_operator_shim_without_restart(self):
        self.install()
        target = (self.root / 'current').resolve()
        entry = target / 'bin/hub'
        entry.unlink()
        entry.write_text('old wheel entry point')
        host_entry = target / 'bin/hub2'
        host_entry.write_text('retired host entry point')
        self.calls.clear()
        self.install()
        self.assertEqual(os.readlink(entry), '../hub.pyz')
        self.assertFalse(host_entry.exists(), 'there is no separate hub2 CLI')
        self.assertFalse(any('systemctl' in args or 'pip' in args for args in self.calls))
        self.calls.clear()
        self.install()
        self.assertEqual(self.calls, [])

    def test_same_version_refuses_corrupt_zipapp_before_replacing_shim(self):
        self.install()
        target = (self.root / 'current').resolve()
        entry = target / 'bin/hub'
        entry.unlink()
        entry.write_text('old wheel entry point')
        (target / 'hub.pyz').write_bytes(b'corrupt')
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, 'installed release artifact mismatch: hub.pyz'):
            self.install()
        self.assertEqual(entry.read_text(), 'old wheel entry point')
        self.assertEqual(self.calls, [])

    def test_activated_shell_uses_operator_zipapp_with_host_package_present(self):
        target = self.root / 'releases' / SHA
        target.parent.mkdir(parents=True)
        venv_argv = [sys.executable, '-m', 'venv', '--without-pip', str(target)]
        venv_env = self.owned_child_env()
        with self.owned_child(lambda argv, kwargs: argv == venv_argv
                              and kwargs.get('check') is True and kwargs.get('cwd') is None
                              and kwargs.get('env') == venv_env
                              and sorted(kwargs) == ['check', 'env', 'stderr', 'stdout']):
            subprocess.run(venv_argv, check=True, env=venv_env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # Build the real operator CLI, then reproduce the host wheel's entry
        # points. The host package is installed only into this venv's
        # site-packages; the sanitized child environment keeps the activated
        # shell from resolving any worker source instead.
        with patch('sys.stdout', new=io.StringIO()):
            module('build-hub-cli').build(target, '0.2.0')
        entry = target / 'bin/hub'
        entry.write_text(f'#!{target}/bin/python\nfrom agent_hub_host.cli import main\nraise SystemExit(main())\n')
        entry.chmod(0o755)
        # The daemon module `hub host` runs lives in the release venv (isolated -I).
        site, = (target / 'lib').glob('python*/site-packages')
        shutil.copytree(REPO / 'hub2/host/agent_hub_host', site / 'agent_hub_host',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        data = (target / 'hub.pyz').read_bytes()
        manifest = {'artifacts': {'hub.pyz': {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}}}
        with patch.object(updater, 'command', self.command):
            updater.install_operator_cli(target, manifest)
        (self.root / 'current').symlink_to(target)
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get('Authorization')))
                payload = {'sessions': [], 'has_more': False} if self.path.startswith('/api/sessions') else {}
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            profile = Path(self.temporary.name) / 'operator.json'
            profile.write_text('{}')
            profile.chmod(0o600)
            env = self.owned_child_env(HOME=self.temporary.name,
                                       HUB_CONFIG_FILE=str(profile),
                                       HUB_URL=f'http://127.0.0.1:{server.server_port}',
                                       HUB_TOKEN='synthetic-operator-token')
            shell_argv = ['/bin/sh', '-ec',
                          '. "$1/bin/activate"; command -v hub; hub --version; hub sessions list; hub host --help',
                          'activated-shell', str(self.root / 'current')]
            with self.owned_child(lambda argv, kwargs: argv == shell_argv
                                  and kwargs.get('cwd') is None
                                  and kwargs.get('env') == env
                                  and sorted(kwargs) == ['capture_output', 'env', 'text', 'timeout']):
                result = subprocess.run(shell_argv, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(str(target / 'bin/hub'), result.stdout)
            self.assertIn('hub 0.2.0', result.stdout)
            self.assertIn('usage: hub host', result.stdout)
            self.assertNotIn('hub2', result.stdout.replace('agent-hub2', ''))
            self.assertTrue(any(path.startswith('/api/sessions') and token == 'Bearer synthetic-operator-token'
                                for path, token in requests), requests)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def prepare_retained_switch(self):
        current = self.old_release()
        self.install(install_service=True)
        retained = (self.root / 'current').resolve()
        startup = retained / 'lib/python3.11/site-packages/retained.pth'
        startup.parent.mkdir(parents=True)
        startup.write_text('import unverified_retained_code')
        updater.link(current, self.root / 'current')
        updater.link(retained, self.root / 'previous')
        units = Path(self.temporary.name) / '.config/systemd/user'
        (units / updater.UNITS[0]).write_bytes(b'active service unit')
        (self.root / 'updater.py').write_bytes(b'current updater')
        self.calls.clear()
        self.downloads.clear()
        return current, retained, units

    def test_retained_preparation_rechecks_sessions_without_restart_on_deferral(self):
        current, retained, units = self.prepare_retained_switch()
        before = (retained / 'release.json').read_bytes()
        self.policy()
        with patch.object(updater, 'managed_session_count', side_effect=[0, 1]), \
             patch.object(updater.time, 'time', return_value=updater.ACTIVATION_INTERVAL + 1):
            self.install(scheduled=True)
        self.assertEqual((self.root / 'current').resolve(), current)
        self.assertEqual((self.root / 'previous').resolve(), retained)
        self.assertEqual((retained / 'release.json').read_bytes(), before)
        self.assertTrue((retained / 'lib/python3.11/site-packages/retained.pth').is_file())
        self.assertEqual((units / updater.UNITS[0]).read_bytes(), b'active service unit')
        self.assertFalse(any('restart' in args or 'stop' in args for args in self.calls))
        self.assertFalse((self.root / 'retained-switch.json').exists())
        self.assertFalse(retained.with_name('.retained-' + retained.name).exists())
        self.assertFalse((self.root / 'failed-releases.json').exists())
        self.assertEqual(self.receipt()['reason'], 'managed_sessions_running')

    def test_rebuilds_retained_release_without_executing_it_or_replacing_updater(self):
        current, retained, units = self.prepare_retained_switch()
        receipt = (retained / 'release.json').read_bytes()
        startup = retained / 'lib/python3.11/site-packages/retained.pth'
        previous_command = self.command

        def assert_clean_command(args):
            if Path(args[0]).is_relative_to(retained):
                self.assertFalse(startup.exists())
            previous_command(args)

        self.command = assert_clean_command
        self.install()
        self.assertFalse(startup.exists())
        self.assertEqual((self.root / 'current').resolve(), retained)
        self.assertEqual((self.root / 'previous').resolve(), current)
        self.assertEqual((retained / 'release.json').read_bytes(), receipt)
        self.assertEqual((self.root / 'updater.py').read_bytes(), b'current updater')
        self.assertEqual((units / updater.UNITS[0]).read_bytes(), self.files[updater.UNITS[0]])
        self.assertEqual(self.downloads, ['host-release.json', updater.UNITS[0], HOST,
                                          'websockets-16.0-py3-none-any.whl', 'hub.pyz', 'hub2-enroll.py',
                                          'host-release.json'])
        self.assertEqual(sum('restart' in args for args in self.calls), 1)
        self.assertTrue(any('venv' in args for args in self.calls))
        self.assertFalse((self.root / 'retained-switch.json').exists())
        self.assertFalse((self.root / 'retained-switch-committed.json').exists())
        self.assertFalse(retained.with_name('.retained-' + retained.name).exists())

    def test_retained_release_readiness_failure_restores_units_and_both_links(self):
        current, retained, units = self.prepare_retained_switch()
        self.status_rc = 1
        startup = retained / 'lib/python3.11/site-packages/retained.pth'
        previous_command = self.command

        def assert_quiesced_before_restore(args):
            if args[2:3] == ['stop']:
                self.assertFalse(startup.exists())
            previous_command(args)

        self.command = assert_quiesced_before_restore
        with self.assertRaisesRegex(RuntimeError, 'host daemon connection not verified'):
            self.install()
        self.assertIn(['systemctl', '--user', 'stop', updater.UNITS[0]], self.calls)
        self.assertEqual((self.root / 'current').resolve(), current)
        self.assertEqual((self.root / 'previous').resolve(), retained)
        self.assertEqual((units / updater.UNITS[0]).read_bytes(), b'active service unit')
        self.assertTrue((retained / 'lib/python3.11/site-packages/retained.pth').is_file())
        self.assertEqual(sum('restart' in args for args in self.calls), 2)
        self.assertFalse((self.root / 'retained-switch.json').exists())
        self.assertIn(SHA, json.loads((self.root / 'failed-releases.json').read_text()))

    def legacy_bundle(self):
        """A verified pre-unification release: its wheel declares the private hub2 entry."""
        wheel_name = f'agent_hub_host_v2-2.0.0+g{OLD_SHA}-py3-none-any.whl'
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('agent_hub_host/_release.py', f'HOST_VERSION = {OLD_SHA!r}\nPROTOCOL_VERSION = 1\n')
            archive.writestr('agent_hub_host_v2-2.0.0.dist-info/entry_points.txt',
                             '[console_scripts]\nhub2 = agent_hub_host.cli:main\nhub = agent_hub_host.cli:main\n')
        unit = f'[Service]\nExecStart={self.root}/current/bin/hub2 serve\n'
        files = {name: data for name, data in self.files.items() if name != HOST}
        files[wheel_name] = buffer.getvalue()
        files[updater.UNITS[0]] = unit.encode()
        manifest = dict(self.manifest, host_version=OLD_SHA,
                        artifacts={name: {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
                                   for name, data in files.items()})
        return wheel_name, files, manifest

    def release_venv(self, target):
        """A real isolated release venv with an explicitly symlinked
        bin/python (stdlib EnvBuilder defaults to copying), so the lexical
        venv path and the resolved interpreter genuinely differ, as in a
        pip-installed release environment."""
        target.mkdir(parents=True, exist_ok=True)
        venv.create(str(target), with_pip=False, system_site_packages=False, symlinks=True)
        python = target / 'bin/python'
        self.assertTrue(python.is_symlink())
        self.assertNotEqual(os.path.realpath(python), os.path.abspath(python))
        return target

    def venv_only_host_package(self, target):
        """A minimal owned agent_hub_host package installed only in this
        venv's site-packages. The base interpreter the launcher's bin/python
        links to cannot import it, so a launcher that runs and imports it
        proves the lexical venv prefix resolved."""
        site = target / 'lib' / ('python%d.%d' % sys.version_info[:2]) / 'site-packages'
        package = site / 'agent_hub_host'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('')
        (package / 'cli.py').write_text(
            'import sys\n'
            'def main(*args):\n'
            '    print(sys.prefix)\n'
            '    print(__file__)\n'
            '    return 0\n')
        return package

    def run_owned_launcher(self, argv):
        """Execute a release launcher under an exact owned-child allowance.

        The synthetic main in venv_only_host_package prints its interpreter
        prefix and module path and exits; no service, provider or network is
        reachable from that child. The sanitized environment is bound in
        full: ambient PYTHONPATH/PYTHONHOME cannot pull the worker's real
        source package into the launcher's interpreter, so a proven run
        imports the venv-only fixture package, not source."""
        launcher_env = self.owned_child_env()
        with self.owned_child(lambda a, kwargs: a == [str(value) for value in argv]
                              and kwargs.get('cwd') == str(self.temporary.name)
                              and kwargs.get('env') == launcher_env
                              and sorted(kwargs) == ['capture_output', 'cwd', 'env', 'text', 'timeout']):
            return subprocess.run([str(value) for value in argv], cwd=str(self.temporary.name),
                                  env=launcher_env, capture_output=True, text=True, timeout=60)

    def pip_launcher_bytes(self, target):
        """The genuine pip console-script form: the shebang names the lexical
        release venv interpreter, never the base interpreter it links to."""
        return ('#!' + os.path.abspath(target / 'bin/python') + '\n'
                '# -*- coding: utf-8 -*-\n'
                'import re\n'
                'import sys\n'
                'from agent_hub_host.cli import main\n'
                "if __name__ == '__main__':\n"
                "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
                '    sys.exit(main())\n').encode()

    def rebuilt_launcher_bytes(self, target):
        """The minimal lexical-venv daemon launcher written when the legacy
        bin/hub2 entry is absent and rebuilt from the release's declared
        metadata."""
        return ('#!' + os.path.abspath(target / 'bin/python') + '\n'
                'import sys\n'
                'from agent_hub_host.cli import main\n'
                "if __name__ == '__main__':\n"
                '    sys.exit(main())\n').encode()

    def test_legacy_wheel_prepares_hub2_launcher_and_restart_selection(self):
        wheel_name, files, manifest = self.legacy_bundle()
        target = self.root / 'releases' / OLD_SHA
        target.mkdir(parents=True)
        self.release_venv(target)
        download = Path(self.temporary.name) / 'download-legacy'
        download.mkdir()
        for name, data in files.items():
            (download / name).write_bytes(data)
        dropin = Path(self.temporary.name) / '.config/systemd/user' / (updater.UNITS[0] + '.d') / '90-host.conf'
        dropin.parent.mkdir(parents=True)
        legacy_dropin = f'[Service]\nExecStart={self.root}/current/bin/hub2 serve --aplexer /x/a\n'
        dropin.write_text(legacy_dropin)
        current = self.root / 'current'
        with patch.object(updater, 'command', self.command), \
             patch.object(Path, 'home', return_value=Path(self.temporary.name)):
            updater.prepare_environment(target, download, manifest, wheel_name)
            # The verified old wheel declares the private daemon entry: when
            # absent from the mocked install, the minimal lexical-venv
            # launcher is rebuilt.
            launcher = target / 'bin/hub2'
            self.assertEqual(launcher.read_bytes(), self.rebuilt_launcher_bytes(target))
            self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
            self.assertEqual(os.readlink(target / 'bin/hub'), '../hub.pyz')
            # restart selection: the legacy release keeps the hub2 drop-in form.
            current.symlink_to(target)
            updater.restart_host(self.root)
            self.assertEqual(dropin.read_text(), legacy_dropin)
            # Inverse compatibility: a unified current release flips the drop-in.
            other = self.root / 'releases' / SHA
            (other / 'bin').mkdir(parents=True)
            current.unlink()
            current.symlink_to(other)
            updater.restart_host(self.root)
            self.assertIn(f'{self.root}/current/bin/hub host serve --aplexer /x/a', dropin.read_text())
            self.assertNotIn('/bin/hub2', dropin.read_text())
            # ...and a rollback to the retained legacy release flips it back.
            current.unlink()
            current.symlink_to(target)
            updater.restart_host(self.root)
            self.assertEqual(dropin.read_text(), legacy_dropin)

    def test_retained_reactivation_rebuilds_legacy_launcher_and_selection(self):
        wheel_name, files, manifest = self.legacy_bundle()
        default_files, default_manifest = self.files, self.manifest
        self.files, self.manifest = files, manifest
        # The readiness probe runs against whichever release is activating.
        def tolerant_probe(args, *, stdout, stderr, timeout):
            version = Path(args[0]).parents[1].name
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(dict(self.status, host_version=version)).encode())
        self.status_command = tolerant_probe
        # CI-equivalent environment: no managed Claude profile is configured.
        self.repin_gate = patch.object(updater, 'managed_claude_configured', return_value=False)
        self.repin_gate.start()
        self.addCleanup(self.repin_gate.stop)
        self.install(install_service=True)
        legacy = (self.root / 'current').resolve()
        self.assertEqual(legacy.name, OLD_SHA)
        self.assertEqual((legacy / 'bin/hub2').read_bytes(), self.rebuilt_launcher_bytes(legacy))
        # A newer unified release becomes current; the legacy release is retained.
        self.files, self.manifest = default_files, default_manifest
        del self.status_command
        self.install()
        self.assertEqual((self.root / 'current').resolve().name, SHA)
        self.assertEqual((self.root / 'previous').resolve(), legacy)
        dropin = Path(self.temporary.name) / '.config/systemd/user' / (updater.UNITS[0] + '.d') / '90-host.conf'
        dropin.parent.mkdir(parents=True)
        dropin.write_text(f'[Service]\nExecStart={self.root}/current/bin/hub host serve\n')
        units = Path(self.temporary.name) / '.config/systemd/user'
        self.assertNotIn(b'/bin/hub2', (units / updater.UNITS[0]).read_bytes())
        # Reactivating the retained release rebuilds it from its verified old
        # wheel and restores the hub2 launcher, unit and drop-in contract.
        self.files, self.manifest = files, manifest
        self.status_command = tolerant_probe
        self.install()
        self.assertEqual((self.root / 'current').resolve(), legacy)
        self.assertEqual((legacy / 'bin/hub2').read_bytes(), self.rebuilt_launcher_bytes(legacy))
        self.assertEqual(os.readlink(legacy / 'bin/hub'), '../hub.pyz')
        self.assertEqual((units / updater.UNITS[0]).read_bytes(), files[updater.UNITS[0]])
        self.assertIn(f'{self.root}/current/bin/hub2 serve', dropin.read_text())

    def test_same_version_repair_keeps_declared_legacy_launcher(self):
        target = self.root / 'releases' / OLD_SHA
        self.release_venv(target)
        (target / 'hub.pyz').write_bytes(b'artifact:hub.pyz')
        launcher = target / 'bin/hub2'
        launcher.write_bytes(self.pip_launcher_bytes(target))
        record = {'sha256': hashlib.sha256(b'artifact:hub.pyz').hexdigest(), 'size': len(b'artifact:hub.pyz')}
        manifest = {'artifacts': {'hub.pyz': record}}
        # The installed environment declares the legacy entry (dist-info of the
        # old wheel), so repair must keep the launcher byte-identical.
        entry_points = target / 'lib/python3.11/site-packages/agent_hub_host_v2-2.0.0.dist-info/entry_points.txt'
        entry_points.parent.mkdir(parents=True)
        entry_points.write_text('[console_scripts]\nhub2 = agent_hub_host.cli:main\n')
        with patch.object(updater, 'command', self.command):
            updater.install_operator_cli(target, manifest)
        self.assertEqual(launcher.read_bytes(), self.pip_launcher_bytes(target))
        self.assertEqual(os.readlink(target / 'bin/hub'), '../hub.pyz')
        # A launcher lost to an older buggy repair is rebuilt, not left missing.
        launcher.unlink()
        with patch.object(updater, 'command', self.command):
            updater.install_operator_cli(target, manifest)
        self.assertEqual(launcher.read_bytes(), self.rebuilt_launcher_bytes(target))
        self.assertEqual(launcher.stat().st_mode & 0o777, 0o755)
        # An unmanaged launcher path is refused, never replaced.
        launcher.unlink()
        launcher.symlink_to('/etc/passwd')
        with patch.object(updater, 'command', self.command):
            with self.assertRaisesRegex(ValueError, 'unmanaged legacy hub2 launcher'):
                updater.install_operator_cli(target, manifest)
        self.assertTrue(launcher.is_symlink())

    def test_resolved_base_interpreter_shebang_is_refused(self):
        # A launcher written by the old buggy repair names the base interpreter
        # that bin/python links to; executing it would leave the release venv
        # where the retained wheel is not installed. That is an unrecognized
        # contract: refused, never rewritten in place.
        target = self.root / 'releases' / OLD_SHA
        self.release_venv(target)
        (target / 'hub.pyz').write_bytes(b'artifact:hub.pyz')
        record = {'sha256': hashlib.sha256(b'artifact:hub.pyz').hexdigest(), 'size': len(b'artifact:hub.pyz')}
        manifest = {'artifacts': {'hub.pyz': record}}
        entry_points = target / 'lib/python3.11/site-packages/agent_hub_host_v2-2.0.0.dist-info/entry_points.txt'
        entry_points.parent.mkdir(parents=True)
        entry_points.write_text('[console_scripts]\nhub2 = agent_hub_host.cli:main\n')
        launcher = target / 'bin/hub2'
        resolved = os.path.realpath(target / 'bin/python')
        self.assertNotEqual(resolved, os.path.abspath(target / 'bin/python'))
        launcher.write_bytes(('#!' + resolved + '\n'
                              'import sys\n'
                              'from agent_hub_host.cli import main\n'
                              "if __name__ == '__main__':\n"
                              '    sys.exit(main())\n').encode())
        with patch.object(updater, 'command', self.command):
            with self.assertRaisesRegex(ValueError, 'unrecognized legacy hub2 launcher'):
                updater.install_operator_cli(target, manifest)
        self.assertTrue(launcher.read_text().startswith('#!' + resolved))

    def assert_launcher_runs_in_its_venv(self, target, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        prefix, module_file, *rest = result.stdout.splitlines()
        self.assertEqual(rest, [])
        self.assertEqual(Path(prefix).resolve(), target.resolve())
        self.assertTrue(Path(module_file).resolve().is_relative_to(target.resolve()))

    def test_pip_form_launcher_runs_inside_its_venv(self):
        # The genuine pip console-script launcher executes inside the release
        # venv: the shebang names the lexical venv interpreter and the only
        # agent_hub_host package is installed in that venv's site-packages,
        # so a successful run proves the prefix, not the base interpreter.
        target = self.release_venv(self.root / 'releases' / OLD_SHA)
        self.venv_only_host_package(target)
        launcher = target / 'bin/hub2'
        launcher.write_bytes(self.pip_launcher_bytes(target))
        launcher.chmod(0o755)
        probe = 'import agent_hub_host; print(agent_hub_host.__file__)'
        probe_argv = [sys.executable, '-I', '-c', probe]
        probe_env = self.owned_child_env()
        with self.owned_child(lambda argv, kwargs: argv == probe_argv
                              and kwargs.get('cwd') == str(self.temporary.name)
                              and kwargs.get('env') == probe_env
                              and sorted(kwargs) == ['capture_output', 'cwd', 'env', 'text']):
            base = subprocess.run(probe_argv, cwd=str(self.temporary.name), env=probe_env,
                                  capture_output=True, text=True)
        self.assertFalse(base.returncode == 0
                         and Path(base.stdout.strip()).is_relative_to(target.resolve()),
                         base.stdout)
        self.assert_launcher_runs_in_its_venv(
            target, self.run_owned_launcher([target / 'bin/python', launcher]))

    def test_launcher_shebang_executes_release_venv_python(self):
        # The strongest form: the kernel itself resolves the launcher's
        # shebang to the lexical release venv interpreter. The kernel refuses
        # shebang lines over 255 bytes, so checkouts whose absolute path is
        # too long skip this proof (the venv-prefix contract is still proven
        # by the explicit-interpreter execution tests and the shebang
        # content by the byte-identity and refusal tests); short checkouts,
        # including CI, run it for real.
        target = self.release_venv(self.root / 'releases' / OLD_SHA)
        self.venv_only_host_package(target)
        launcher = target / 'bin/hub2'
        launcher.write_bytes(self.pip_launcher_bytes(target))
        launcher.chmod(0o755)
        shebang = launcher.read_bytes().splitlines(True)[0].rstrip(b'\n')
        if len(shebang) > 255:
            self.skipTest('kernel shebang limit (255 bytes) exceeded by this checkout path: '
                          + len(shebang).__str__())
        self.assert_launcher_runs_in_its_venv(target, self.run_owned_launcher([launcher]))

    def test_rebuilt_legacy_launcher_runs_inside_its_venv(self):
        # The launcher regenerated by the repair path from the release's own
        # declared metadata executes with the same venv-prefix guarantee.
        target = self.release_venv(self.root / 'releases' / OLD_SHA)
        self.venv_only_host_package(target)
        (target / 'hub.pyz').write_bytes(b'artifact:hub.pyz')
        record = {'sha256': hashlib.sha256(b'artifact:hub.pyz').hexdigest(), 'size': len(b'artifact:hub.pyz')}
        manifest = {'artifacts': {'hub.pyz': record}}
        entry_points = target / 'lib' / ('python%d.%d' % sys.version_info[:2]) / 'site-packages' / \
            'agent_hub_host_v2-2.0.0.dist-info' / 'entry_points.txt'
        entry_points.parent.mkdir(parents=True)
        entry_points.write_text('[console_scripts]\nhub2 = agent_hub_host.cli:main\n')
        with patch.object(updater, 'command', self.command):
            updater.install_operator_cli(target, manifest)
        launcher = target / 'bin/hub2'
        # The regenerated simple form differs from the pip console-script
        # bytes and still runs inside the venv.
        self.assertNotEqual(launcher.read_bytes(), self.pip_launcher_bytes(target))
        self.assert_launcher_runs_in_its_venv(
            target, self.run_owned_launcher([target / 'bin/python', launcher]))
        self.assertEqual(os.readlink(target / 'bin/hub'), '../hub.pyz')

    def test_fixture_default_deny_blocks_unpatched_process_effects(self):
        # Harmless guard control: with no per-test patch at all, the runtime
        # paths that restarted the live Host in the incident fail closed on
        # the fixture command deny, and an unregistered child is refused
        # outright. Nothing here spawns or touches a service.
        self.old_release()
        with self.assertRaises(AssertionError):
            updater.restart_host(self.root)
        self.assertEqual(self.calls, [])
        with self.assertRaises(AssertionError):
            subprocess.run([str(self.root / 'releases' / SHA / 'bin/python'), '--version'])

    def test_fixture_guard_adapter_executes_exactly_the_registered_children(self):
        # Guard controls under corrected discovery: an allowed run reaches
        # the real spawn through the guarded adapter via an exact one-shot
        # permit, an allowed direct Popen passes the guarded path with its
        # declared capture, a deviation from the registered contract is
        # refused before spawn, and an unregistered child stays denied even
        # while the adapter is active. Children are disposable interpreters
        # in the owned root under the sanitized child environment; nothing
        # here touches a service, provider or network.
        self.root.mkdir(parents=True)
        env = self.owned_child_env()
        argv = [sys.executable, '-I', '-c', "print('guard-ok')"]
        with self.owned_child(lambda a, kwargs: a == argv and kwargs.get('cwd') is None
                              and kwargs.get('env') == env
                              and sorted(kwargs) == ['capture_output', 'env', 'text']):
            result = subprocess.run(argv, env=env, capture_output=True, text=True)
            # staticv3 review R1: the run-child permit is one-shot. While the
            # run allowance is still open, the same argv cannot spawn again
            # through the spent permit: the direct-Popen path re-checks the
            # allowance, whose contract binds only the run call shape.
            with self.assertRaisesRegex(AssertionError, 'unallowed real process'):
                subprocess.Popen(argv, env=env)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, 'guard-ok'))
        with self.owned_child(lambda a, kwargs: a == argv and kwargs.get('cwd') is None
                              and kwargs.get('env') == env and sorted(kwargs) == ['env', 'stdout']):
            with subprocess.Popen(argv, env=env, stdout=subprocess.PIPE) as process:
                stdout, _ = process.communicate(timeout=30)
        self.assertEqual((process.returncode, stdout.decode().strip()), (0, 'guard-ok'))
        # A contract deviation (run-level kwargs the allowance does not bind)
        # is refused by the guard before any spawn.
        with self.owned_child(lambda a, kwargs: a == argv and kwargs.get('cwd') is None
                              and kwargs.get('env') == env and sorted(kwargs) == ['env']):
            with self.assertRaises(AssertionError):
                subprocess.run(argv, env=env, capture_output=True, text=True)
        # staticv3 review R2: positional overrides (bufsize/executable/...)
        # are refused outright before any allowance matching, so an approved
        # argv can never be paired with a different positional executable.
        with self.assertRaisesRegex(AssertionError, 'positional arguments beyond the command'):
            subprocess.run(argv, None, env=env)
        with self.assertRaisesRegex(AssertionError, 'positional arguments beyond the command'):
            subprocess.Popen(argv, -1, env=env)
        # staticv4 review R1 causal controls: exercise the actual guarded
        # subprocess.run -> _guarded_run -> _guarded_popen path with harmless
        # fake lower-level hooks (no real children). Different argv, same argv
        # with altered executable/shell/env/cwd, and another thread stealing
        # the exact same argv+kwargs are all refused without consuming the
        # permit; the intended spawn consumes it once BEFORE wait, and
        # unregistered spawns during wait or after consumption fail closed.
        other_argv = [sys.executable, '-I', '-c', "print('must-not-run')"]
        alt_env = dict(env, EXTRA_VAR='tampered')
        alt_cwd = str(self.root)
        spawned_calls = []
        during_wait_errors = []
        other_thread_errors = []
        permit_counts_at_spawn = []

        class FakeProcess:
            def __init__(self, cmd, **kw):
                self.cmd = cmd
                self.kw = kw
                self.returncode = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def communicate(self, input=None, timeout=None):
                for label, call_argv, call_kw in (
                    ('same-argv-during-wait', argv, {'env': env, 'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE, 'text': True}),
                    ('other-argv-during-wait', other_argv, {'env': env}),
                ):
                    try:
                        subprocess.Popen(call_argv, **call_kw)
                    except AssertionError as exc:
                        during_wait_errors.append((label, str(exc)))
                    else:
                        raise AssertionError(f'{label} unexpectedly succeeded during wait')
                return (b'fake-ok\n', b'')

            def poll(self):
                return self.returncode

        def fake_real_popen(cmd, **kw):
            permit_counts_at_spawn.append(len(self._run_spawn_permits))
            spawned_calls.append((list(cmd), dict(kw)))
            return FakeProcess(cmd, **kw)

        def fake_real_run(cmd, **kw):
            popen_kw = self._expected_popen_kwargs(kw)
            self.assertEqual(len(self._run_spawn_permits), 1)
            with self.assertRaisesRegex(AssertionError, 'pending validated'):
                subprocess.Popen(other_argv, **popen_kw)
            self.assertEqual(len(self._run_spawn_permits), 1)
            for tampered_kw in (
                dict(popen_kw, executable='/bin/sh'),
                dict(popen_kw, shell=True),
                dict(popen_kw, env=alt_env),
                dict(popen_kw, cwd=alt_cwd),
            ):
                with self.assertRaisesRegex(AssertionError, 'pending validated'):
                    subprocess.Popen(cmd, **tampered_kw)
                self.assertEqual(len(self._run_spawn_permits), 1)

            def steal_from_other_thread():
                try:
                    subprocess.Popen(cmd, **popen_kw)
                except AssertionError as exc:
                    other_thread_errors.append(str(exc))

            thief = threading.Thread(target=steal_from_other_thread)
            thief.start()
            thief.join(timeout=5)
            self.assertFalse(thief.is_alive())
            self.assertEqual(len(other_thread_errors), 1)
            self.assertIn('pending validated', other_thread_errors[0])
            self.assertEqual(len(self._run_spawn_permits), 1)

            with subprocess.Popen(cmd, **popen_kw) as proc:
                out, err = proc.communicate(timeout=30)
            with self.assertRaisesRegex(AssertionError, 'unallowed real process'):
                subprocess.Popen(cmd, **popen_kw)
            return subprocess.CompletedProcess(cmd, proc.returncode, stdout=out, stderr=err)

        with patch.object(self, '_real_subprocess_run', fake_real_run), \
             patch.object(self, '_real_subprocess_popen', fake_real_popen), \
             self.owned_child(lambda a, kwargs: a == argv and kwargs.get('cwd') is None
                              and kwargs.get('env') == env
                              and sorted(kwargs) == ['capture_output', 'env', 'text']):
            fake_result = subprocess.run(argv, env=env, capture_output=True, text=True)
        self.assertEqual((fake_result.returncode, fake_result.stdout), (0, b'fake-ok\n'))
        self.assertEqual(permit_counts_at_spawn, [0])
        self.assertEqual(len(spawned_calls), 1)
        self.assertEqual(spawned_calls[0][0], argv)
        self.assertEqual(len(during_wait_errors), 2)
        self.assertEqual(self._run_spawn_permits, [])
        # An unregistered child stays denied outright.
        with self.assertRaises(AssertionError):
            subprocess.run([sys.executable, '-I', '-c', "print('refused')"],
                           env=env, capture_output=True, text=True)

    def test_unified_wheel_retires_stale_hub2_launcher(self):
        wheel_name = HOST
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('agent_hub_host/_release.py', f'HOST_VERSION = {SHA!r}\nPROTOCOL_VERSION = 1\n')
            archive.writestr('agent_hub_host-2.0.0.dist-info/entry_points.txt',
                             '[console_scripts]\nhub = agent_hub_host.wheel_cli:main\n')
        files = dict(self.files)
        files[wheel_name] = buffer.getvalue()
        manifest = dict(self.manifest,
                        artifacts={name: {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
                                   for name, data in files.items()})
        target = self.root / 'releases' / SHA
        (target / 'bin').mkdir(parents=True)
        stale = target / 'bin/hub2'
        stale.write_text('#!/bin/sh\nrm -rf ~\n')
        download = Path(self.temporary.name) / 'download-unified'
        download.mkdir()
        for name, data in files.items():
            (download / name).write_bytes(data)
        with patch.object(updater, 'command', self.command), \
             patch.object(Path, 'home', return_value=Path(self.temporary.name)):
            updater.prepare_environment(target, download, manifest, wheel_name)
        self.assertFalse(stale.exists())
        self.assertEqual(os.readlink(target / 'bin/hub'), '../hub.pyz')

    def test_failed_new_activation_restores_legacy_units_and_launcher(self):
        wheel_name, files, _ = self.legacy_bundle()
        old = self.old_release()
        (old / 'bin').mkdir()
        old_launcher = old / 'bin/hub2'
        old_launcher.write_bytes(self.pip_launcher_bytes(old))
        units = Path(self.temporary.name) / '.config/systemd/user'
        units.mkdir(parents=True)
        legacy_unit = f'[Service]\nExecStart={self.root}/current/bin/hub2 serve\n'.encode()
        (units / updater.UNITS[0]).write_bytes(legacy_unit)
        dropin = units / (updater.UNITS[0] + '.d') / '90-host.conf'
        dropin.parent.mkdir()
        legacy_dropin = f'[Service]\nExecStart={self.root}/current/bin/hub2 serve --aplexer /x/a\n'
        dropin.write_text(legacy_dropin)
        self.status_rc = 1  # the new unified release fails its readiness probe
        with self.assertRaisesRegex(RuntimeError, 'host daemon connection not verified'):
            self.install()
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertEqual(old_launcher.read_bytes(), self.pip_launcher_bytes(old))
        self.assertEqual((units / updater.UNITS[0]).read_bytes(), legacy_unit)
        self.assertEqual(dropin.read_text(), legacy_dropin)

    def test_interrupted_retained_switch_recovers_even_when_manifest_is_unavailable(self):
        current, retained, units = self.prepare_retained_switch()
        journal = self.root / 'retained-switch.json'
        updater.write_json(journal, {'current': current.name, 'target': retained.name, 'previous': retained.name,
                                     'units': {name: (units / name).read_bytes().hex() for name in updater.UNITS},
                                     'switching': True})
        retained.rename(retained.with_name('.retained-' + retained.name))
        retained.mkdir()
        # The pointer was already restored, but the candidate may still run.
        updater.link(current, self.root / 'previous')
        (units / updater.UNITS[0]).write_bytes(b'interrupted service unit')

        def offline(*args):
            raise OSError('offline')

        self.fetch = offline
        with self.assertRaisesRegex(OSError, 'offline'):
            self.install()
        self.assertEqual((self.root / 'current').resolve(), current)
        self.assertEqual((self.root / 'previous').resolve(), retained)
        self.assertEqual((units / updater.UNITS[0]).read_bytes(), b'active service unit')
        self.assertFalse(journal.exists())
        self.assertIn(['systemctl', '--user', 'stop', updater.UNITS[0]], self.calls)
        self.assertTrue((retained / 'lib/python3.11/site-packages/retained.pth').is_file())

    def test_retained_build_failure_restores_original_environment(self):
        current, retained, units = self.prepare_retained_switch()
        with patch.object(updater, 'prepare_environment', side_effect=RuntimeError('venv build failed')):
            with self.assertRaisesRegex(RuntimeError, 'venv build failed'):
                self.install()
        self.assertEqual((self.root / 'current').resolve(), current)
        self.assertEqual((self.root / 'previous').resolve(), retained)
        self.assertEqual((units / updater.UNITS[0]).read_bytes(), b'active service unit')
        self.assertTrue((retained / 'lib/python3.11/site-packages/retained.pth').is_file())

    def test_committed_retained_switch_resumes_backup_cleanup_without_restarting(self):
        current, retained, _ = self.prepare_retained_switch()
        cleanup = updater.finish_retained_switch
        with patch.object(updater, 'finish_retained_switch', side_effect=OSError('cleanup interrupted')):
            with self.assertRaisesRegex(OSError, 'cleanup interrupted'):
                self.install()
        journal = self.root / 'retained-switch-committed.json'
        backup = retained.with_name('.retained-' + retained.name)
        self.assertTrue(journal.is_file())
        self.assertTrue(backup.is_dir())
        self.calls.clear()
        with patch.object(updater, 'finish_retained_switch', wraps=cleanup) as resumed:
            self.install()
        resumed.assert_called_once()
        self.assertEqual((self.root / 'current').resolve(), retained)
        self.assertEqual((self.root / 'previous').resolve(), current)
        self.assertFalse(backup.exists())
        self.assertFalse(journal.exists())
        self.assertEqual(self.calls, [])

    def test_resumed_cleanup_syncs_committed_decision_before_discarding_backup(self):
        _, retained, _ = self.prepare_retained_switch()
        journal = self.root / 'retained-switch-committed.json'
        backup = retained.with_name('.retained-' + retained.name)
        sync = updater.sync_directory

        def interrupt_sync(directory):
            if directory == self.root and journal.exists():
                self.assertTrue(backup.is_dir())
                raise OSError('interrupted commit sync')
            sync(directory)

        with patch.object(updater, 'sync_directory', side_effect=interrupt_sync):
            with self.assertRaisesRegex(OSError, 'interrupted commit sync'):
                self.install()
        self.assertTrue(journal.exists())
        self.assertTrue(backup.is_dir())
        observed = []

        def track_sync(directory):
            observed.append((directory, backup.is_dir()))
            sync(directory)

        with patch.object(updater, 'sync_directory', side_effect=track_sync):
            self.install()
        self.assertEqual(observed[0], (self.root, True))
        self.assertIn((retained.parent, False), observed)
        self.assertFalse(journal.exists())

    def test_committed_cleanup_resyncs_releases_if_backup_is_already_removed(self):
        _, retained, _ = self.prepare_retained_switch()
        sync = updater.sync_directory
        journal = self.root / 'retained-switch-committed.json'
        backup = retained.with_name('.retained-' + retained.name)

        def interrupt_sync(directory):
            if directory == retained.parent and journal.exists() and not backup.exists():
                raise OSError('interrupted directory sync')
            sync(directory)

        with patch.object(updater, 'sync_directory', side_effect=interrupt_sync):
            with self.assertRaisesRegex(OSError, 'interrupted directory sync'):
                self.install()
        self.assertTrue(journal.exists())
        self.assertFalse(backup.exists())
        with patch.object(updater, 'sync_directory', wraps=sync) as synced:
            self.install()
        synced.assert_any_call(retained.parent)
        self.assertFalse(journal.exists())

    def test_killed_updater_leaves_lock_held_until_installer_exits(self):
        self.root.mkdir()
        started = self.root / 'child-started'
        release = self.root / 'child-may-exit'
        child = """
from pathlib import Path
import sys
import time

Path(sys.argv[1]).touch()
deadline = time.monotonic() + 10
while not Path(sys.argv[2]).exists() and time.monotonic() < deadline:
    time.sleep(.02)
"""
        parent = """
from pathlib import Path
import runpy
import sys

api = runpy.run_path(sys.argv[1])
with api['update_lock'](Path(sys.argv[2])):
    api['command']([sys.executable, '-c', sys.argv[3], *sys.argv[4:]])
"""
        lock_argv = [sys.executable, '-c', parent, str(REPO / 'scripts/hub2-self-update.py'),
                     str(self.root), child, str(started), str(release)]
        # The allowance binds the whole direct Popen contract; the lock
        # parent's own grandchild (the sleeping child script) is spawned
        # inside that owned interpreter, inherits the same sanitized
        # environment and only touches the owned started/release markers.
        lock_env = self.owned_child_env()
        with self.owned_child(lambda argv, kwargs: argv == lock_argv and kwargs.get('cwd') is None
                              and kwargs.get('env') == lock_env and sorted(kwargs) == ['env']):
            with subprocess.Popen(lock_argv, env=lock_env) as process:
                try:
                    deadline = time.monotonic() + 5
                    while not started.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(.02)
                    self.assertTrue(started.exists())
                    process.kill()
                    process.wait(timeout=5)
                    with self.assertRaises(BlockingIOError), updater.update_lock(self.root):
                        self.fail('orphan installer did not hold the update lock')
                finally:
                    release.touch()
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
        deadline = time.monotonic() + 5
        while True:
            try:
                with updater.update_lock(self.root):
                    break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.02)

    def test_retained_release_refuses_different_receipt_or_tampered_download(self):
        current, retained, _ = self.prepare_retained_switch()
        with patch.dict(self.manifest, {'protocol_version': 2}):
            with self.assertRaisesRegex(ValueError, 'does not match the published manifest'):
                self.install()
        self.files['hub.pyz'] = b'tampered'
        with self.assertRaisesRegex(ValueError, 'checksum mismatch: hub.pyz'):
            self.install()
        self.assertEqual((self.root / 'current').resolve(), current)
        self.assertEqual((self.root / 'previous').resolve(), retained)
        self.assertEqual(self.calls, [])

    def test_retained_activation_rechecks_published_manifest_after_socket_readiness(self):
        current, retained, units = self.prepare_retained_switch()
        fetch = self.fetch

        def changed(url, limit):
            if url.endswith('/host-release.json') and 'host-release.json' in self.downloads:
                return b'{}'
            return fetch(url, limit)

        self.fetch = changed
        with self.assertRaisesRegex(ValueError, 'published host release changed'):
            self.install()
        self.assertEqual((self.root / 'current').resolve(), current)
        self.assertEqual((self.root / 'previous').resolve(), retained)
        self.assertEqual((units / updater.UNITS[0]).read_bytes(), b'active service unit')

    def test_existing_uv_creates_venv_without_system_ensurepip(self):
        mock_uv = str(Path(self.temporary.name) / 'mock-uv')
        with patch.object(updater.shutil, 'which', return_value=mock_uv):
            self.install()
        self.assertTrue(any(args[:3] == [mock_uv, 'venv', '--allow-existing'] for args in self.calls))
        self.assertTrue(any(args[:3] == [mock_uv, 'pip', 'install'] and '--no-index' in args for args in self.calls))

    def test_stdlib_fallback_when_uv_unavailable(self):
        with patch.object(updater.shutil, 'which', return_value=None):
            self.install()
        self.assertTrue(any(args[1:3] == ['-m', 'venv'] for args in self.calls))

    def test_ambient_uv_on_path_is_never_discovered_or_executed_by_install(self):
        # Causal regression for CI run 44481800454 at bd7761f74: with a real
        # uv present on the ambient PATH (GitHub runners preinstall it), the
        # actual product install path must never discover or execute it and
        # must keep the deterministic offline stdlib fallback. The marker is
        # a harmless owned script: discovery that leaked back to ambient
        # PATH would resolve this exact path and the provisioning commands
        # below would contain it, and any real execution is refused by the
        # process guard before spawn and would leave the loud flag file.
        ambient_bin = Path(self.temporary.name) / 'ambient-uv-bin'
        ambient_bin.mkdir()
        marker = ambient_bin / 'uv'
        executed_flag = Path(self.temporary.name) / 'ambient-uv-executed'
        marker.write_text('#!/bin/sh\ntouch "$AMBIENT_UV_EXECUTED_FLAG"\nexit 42\n')
        marker.chmod(0o700)
        old = self.old_release()
        with patch.dict(os.environ, PATH=str(ambient_bin) + os.pathsep + os.environ['PATH'],
                        AMBIENT_UV_EXECUTED_FLAG=str(executed_flag)):
            self.install()
        self.assertFalse(executed_flag.exists(), 'the ambient uv marker was executed')
        self.assertFalse(any(str(marker) in args for args in self.calls),
                         'ambient PATH leaked into provisioning discovery')
        self.assertFalse(any(Path(args[0]).name == 'uv' for args in self.calls),
                         'an external uv command reached the install path')
        self.assertTrue(any(args[1:3] == ['-m', 'venv'] for args in self.calls))
        pip = next(args for args in self.calls if 'pip' in args)
        self.assertIn('--no-index', pip)
        self.assertIn('--no-deps', pip)
        self.assertEqual((self.root / 'previous').resolve(), old)
        self.assertEqual((self.root / 'current').resolve().name, SHA)

    def test_corrupt_artifact_never_touches_current_or_installs(self):
        old = self.old_release()
        self.files[HOST] = b'tampered'
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.install()
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / 'releases' / SHA).exists())

    def test_restart_failure_restores_previous_and_removes_only_new_release(self):
        old = self.old_release()
        original = self.command

        def fail_restart(args):
            original(args)
            if [str(value) for value in args] == ['systemctl', '--user', 'is-active', '--quiet', 'agent-hub2-host.service']:
                raise RuntimeError('failed restart')

        self.command = fail_restart
        with self.assertRaisesRegex(RuntimeError, 'failed restart'):
            self.install()
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertTrue(old.exists())
        self.assertFalse((self.root / 'releases' / SHA).exists())
        self.assertEqual(sum('restart' in args for args in self.calls), 2)

    def test_delayed_service_failure_rolls_back_and_next_tick_backs_off(self):
        old = self.old_release()
        checks = 0
        def delayed(args):
            nonlocal checks
            self.calls.append([str(value) for value in args])
            if 'is-active' in args:
                checks += 1
                if checks == 5:
                    raise RuntimeError('post-start crash')
        self.command = delayed
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual(checks, 5)
        self.assertEqual((self.root / 'current').resolve(), old)
        self.calls.clear()
        self.install()
        self.assertEqual(self.calls, [])
        self.assertEqual(json.loads((self.root / 'failed-releases.json').read_text())[SHA]['attempts'], 1)

    def test_interrupted_marked_install_and_pending_link_are_recovered(self):
        old = self.old_release()
        partial = self.root / 'releases' / SHA
        partial.mkdir()
        (partial / '.installing').write_text(SHA)
        (self.root / 'current.next').symlink_to(partial)
        self.install()
        self.assertFalse(partial.exists())
        self.assertFalse((self.root / 'current.next').is_symlink())
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertTrue((self.root / 'failed-releases.json').exists())

    def test_installed_units_refresh_on_timer_update(self):
        self.old_release()
        units = Path(self.temporary.name) / '.config/systemd/user'
        units.mkdir(parents=True)
        (units / updater.UNITS[0]).write_text('old unit')
        self.install()
        self.assertEqual((units / updater.UNITS[0]).read_bytes(), self.files[updater.UNITS[0]])
        self.assertTrue(any('daemon-reload' in args for args in self.calls))

    def test_failed_activation_restores_unit_contents(self):
        self.old_release()
        units = Path(self.temporary.name) / '.config/systemd/user'
        units.mkdir(parents=True)
        (units / updater.UNITS[0]).write_text('original unit')
        def failure(args):
            if 'is-active' in args:
                raise RuntimeError('failed activation')
        self.command = failure
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertEqual((units / updater.UNITS[0]).read_text(), 'original unit')
        self.assertFalse((units / updater.UNITS[1]).exists())

    def test_interrupted_activation_reverts_current_before_retry(self):
        old = self.old_release()
        partial = self.root / 'releases' / SHA
        partial.mkdir()
        (partial / '.installing').write_text(SHA)
        (self.root / 'previous').symlink_to(old)
        (self.root / 'current').unlink()
        (self.root / 'current').symlink_to(partial)
        self.install()
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertTrue(any('restart' in args for args in self.calls))
        self.assertFalse(partial.exists())

    def test_new_protocol_is_installed_when_manifest_and_wheel_match(self):
        self.manifest['protocol_version'] = 2
        self.status['protocol_version'] = 2
        self.install()
        check = next(args for args in self.calls if '-c' in args)
        self.assertEqual(check[1:3], ['-I', '-c'])
        self.assertEqual(check[4:], [SHA, '2', str((self.root / 'releases' / SHA).resolve())])

    def test_prune_keeps_three_owned_releases_and_unmanaged_directory(self):
        self.old_release()
        for letter in ('c', 'd', 'e', 'f'):
            path = self.root / 'releases' / (letter * 40)
            path.mkdir()
            (path / 'release.json').write_text(json.dumps({'host_version': path.name}))
        unmanaged = self.root / 'releases/owner-work'
        unmanaged.mkdir()
        self.install()
        managed = [path for path in (self.root / 'releases').iterdir() if path.name != 'owner-work']
        self.assertEqual(len(managed), 3)
        self.assertTrue(unmanaged.exists())

    def test_first_install_failure_removes_current_and_new_environment(self):
        def failure(args):
            if 'restart' in args:
                raise RuntimeError('cannot start')
        self.command = failure
        with self.assertRaises(RuntimeError):
            self.install()
        self.assertFalse((self.root / 'current').is_symlink())
        self.assertFalse((self.root / 'releases' / SHA).exists())

    def provider_bin(self, directory, *commands):
        directory = Path(self.temporary.name) / directory
        directory.mkdir(parents=True)
        for name in commands:
            executable = directory / name
            executable.write_text('#!/bin/sh\nexit 0\n')
            executable.chmod(0o700)
        return directory

    def test_service_install_preserves_discovered_provider_and_runtime_directories(self):
        versioned = self.provider_bin('nvm/versions/node/v99.12.3/bin', 'codex', 'claude', 'opencode', 'node', 'npm')
        wrappers = self.provider_bin('provider wrappers', 'zcodex', 'agy', 'a', 'aplexer')
        irrelevant = self.provider_bin('unrelated', 'unrelated-command')
        path = os.pathsep.join(map(str, (wrappers, irrelevant, versioned, wrappers)))
        env = Path(self.temporary.name) / '.config/agent-hub/host.env'
        original_command = self.command

        def check_before_restart(args):
            if 'restart' in args:
                self.assertTrue(env.is_file())
            original_command(args)

        self.command = check_before_restart
        with patch.dict(os.environ, PATH=path, SYNTHETIC_PRIVATE_TOKEN='do-not-copy'):
            self.install(install_service=True)
        expected = [str(wrappers), str(versioned), str(Path(self.temporary.name) / '.local/bin'),
                    '/usr/local/bin', '/usr/bin', '/bin']
        text = env.read_text()
        self.assertIn('PATH="' + os.pathsep.join(expected) + '"\n', text)
        self.assertNotIn(str(irrelevant), text)
        self.assertNotIn('SYNTHETIC_PRIVATE_TOKEN', text)
        self.assertNotIn('do-not-copy', text)
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        for name in updater.PROVIDER_PATH_COMMANDS:
            self.assertIsNotNone(shutil.which(name, path=os.pathsep.join(expected)))

    def test_service_install_discovers_isolated_default_native_a_executable(self):
        native = self.provider_bin('native-only/bin', 'a')
        with patch.dict(os.environ, PATH=str(native)):
            self.install(install_service=True, restart=False)
        text = (self.config.parent / 'host.env').read_text()
        self.assertIn('PATH="' + str(native) + ':', text)
        self.assertIsNotNone(shutil.which('a', path=str(native)))
        self.assertIsNone(shutil.which('aplexer', path=str(native)))

    def test_current_service_restarts_when_environment_is_first_published(self):
        self.install()
        self.calls.clear()
        self.install(install_service=True)
        restart = ['systemctl', '--user', 'restart', updater.UNITS[0]]
        self.assertIn(restart, self.calls)
        self.assertNotIn(['systemctl', '--user', 'start', updater.UNITS[0]], self.calls)
        observation = next(i for i, args in enumerate(self.calls) if args[-1] == STATUS_PATH)
        self.assertLess(self.calls.index(restart), observation)

    def test_current_service_restarts_without_changing_existing_environment(self):
        self.install()
        env = self.config.parent / 'host.env'
        env.write_bytes(b'PATH=/operator/bin:/usr/bin\n')
        self.calls.clear()
        self.install(install_service=True)
        self.assertIn(['systemctl', '--user', 'restart', updater.UNITS[0]], self.calls)
        self.assertNotIn(['systemctl', '--user', 'start', updater.UNITS[0]], self.calls)
        self.assertEqual(env.read_bytes(), b'PATH=/operator/bin:/usr/bin\n')

    def test_current_service_environment_can_be_staged_without_restart(self):
        self.install()
        self.calls.clear()
        self.install(install_service=True, restart=False)
        self.assertTrue((self.config.parent / 'host.env').is_file())
        self.assertFalse(any(args[0] == 'systemctl' and ('start' in args or 'restart' in args or 'enable' in args)
                             for args in self.calls))
        self.assertFalse(any(args[-1] == STATUS_PATH for args in self.calls))

    def test_staged_current_service_environment_is_loaded_on_later_explicit_install(self):
        self.install()
        self.install(install_service=True, restart=False)
        env = self.config.parent / 'host.env'
        staged = env.read_bytes()
        self.calls.clear()
        self.install(install_service=True)
        restart = ['systemctl', '--user', 'restart', updater.UNITS[0]]
        self.assertIn(restart, self.calls)
        observation = next(i for i, args in enumerate(self.calls) if args[-1] == STATUS_PATH)
        self.assertLess(self.calls.index(restart), observation)
        self.assertEqual(env.read_bytes(), staged)

    def test_current_service_retries_restart_after_environment_publication_then_repair_failure(self):
        self.install()
        self.calls.clear()
        with patch.object(updater, 'repair_service_files', side_effect=RuntimeError('synthetic repair failure')):
            with self.assertRaisesRegex(RuntimeError, 'synthetic repair failure'):
                self.install(install_service=True)
        env = self.config.parent / 'host.env'
        published = env.read_bytes()
        self.assertFalse(any(args[0] == 'systemctl' for args in self.calls))
        self.calls.clear()
        self.install(install_service=True)
        restart = ['systemctl', '--user', 'restart', updater.UNITS[0]]
        self.assertIn(restart, self.calls)
        observation = next(i for i, args in enumerate(self.calls) if args[-1] == STATUS_PATH)
        self.assertLess(self.calls.index(restart), observation)
        self.assertEqual(env.read_bytes(), published)

    def test_provider_symlink_keeps_bin_directory_and_quotes_environment_file(self):
        versioned = self.provider_bin('node versions/quoted"bin\\dir', 'node')
        target = self.provider_bin('package/libexec', 'codex') / 'codex'
        (versioned / 'codex').symlink_to(target)
        with patch.object(Path, 'home', return_value=Path(self.temporary.name)), \
             patch.dict(os.environ, PATH=str(versioned)):
            updater.preserve_provider_path()
        text = (self.config.parent / 'host.env').read_text()
        escaped = str(versioned).replace('\\', '\\\\').replace('"', '\\"')
        self.assertIn(escaped + ':', text)
        self.assertNotIn(str(target.parent), text)

    def test_explicit_environment_override_is_untouched_during_service_upgrade_and_failure(self):
        self.old_release()
        env = self.config.parent / 'host.env'
        original = b'PATH=/operator/explicit/bin:/usr/bin\nEXPLICIT_SETTING=keep\n'
        env.write_bytes(original)
        env.chmod(0o640)
        self.status_rc = 1
        with self.assertRaisesRegex(RuntimeError, 'connection not verified'):
            self.install(install_service=True)
        self.assertEqual(env.read_bytes(), original)
        self.assertEqual(env.stat().st_mode & 0o777, 0o640)
        (self.root / 'failed-releases.json').unlink()
        self.status_rc = 0
        self.install(install_service=True)
        self.assertEqual(env.read_bytes(), original)

    def test_existing_empty_or_symlink_environment_is_not_replaced(self):
        env = self.config.parent / 'host.env'
        target = self.config.parent / 'operator.env'
        target.write_bytes(b'PATH=/operator/bin\n')
        with patch.object(Path, 'home', return_value=Path(self.temporary.name)):
            env.touch()
            updater.preserve_provider_path()
            self.assertEqual(env.read_bytes(), b'')
            env.unlink()
            for destination in (target, self.config.parent / 'missing-operator.env'):
                env.symlink_to(destination)
                updater.preserve_provider_path()
                self.assertTrue(env.is_symlink())
                self.assertEqual(env.readlink(), destination)
                env.unlink()
        self.assertEqual(target.read_bytes(), b'PATH=/operator/bin\n')

    def test_provider_environment_publication_does_not_clobber_concurrent_override(self):
        env = self.config.parent / 'host.env'
        real_link = os.link

        def operator_wins(source, destination):
            env.write_bytes(b'PATH=/concurrent/operator/bin\n')
            real_link(source, destination)

        with patch.object(Path, 'home', return_value=Path(self.temporary.name)), \
             patch.object(updater.os, 'link', side_effect=operator_wins):
            updater.preserve_provider_path()
        self.assertEqual(env.read_bytes(), b'PATH=/concurrent/operator/bin\n')
        self.assertEqual(list(self.config.parent.glob('host.env.*')), [])

    def test_generated_provider_environment_survives_normal_update_and_repair(self):
        versioned = self.provider_bin('versioned/bin', 'codex', 'node')
        with patch.dict(os.environ, PATH=str(versioned)):
            self.install(install_service=True, restart=False)
        env = self.config.parent / 'host.env'
        original = env.read_bytes()
        with patch.dict(os.environ, PATH='/usr/bin:/bin'):
            self.install(install_service=True, restart=False)
            # Exercise normal new-release activation with the generated config.
            old = self.root / 'releases' / OLD_SHA
            old.mkdir()
            updater.link(old, self.root / 'current')
            shutil.rmtree(self.root / 'releases' / SHA)
            self.install()
        self.assertEqual(env.read_bytes(), original)
        self.assertIn(str(versioned), env.read_text())

    def test_failed_install_leaves_complete_provider_config_for_recovery(self):
        self.status_rc = 1
        with self.assertRaisesRegex(RuntimeError, 'connection not verified'):
            self.install(install_service=True)
        env = self.config.parent / 'host.env'
        original = env.read_bytes()
        self.assertFalse((self.root / 'current').is_symlink())
        self.assertFalse(any('enable' in args for args in self.calls))
        self.assertEqual(env.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(RuntimeError, 'activation backoff'):
            self.install(install_service=True)
        self.assertEqual(env.read_bytes(), original)

    def test_provider_environment_write_failure_does_not_activate_or_enable_service(self):
        old = self.old_release()
        with patch.object(updater.os, 'link', side_effect=OSError('synthetic publication failure')):
            with self.assertRaisesRegex(OSError, 'synthetic publication failure'):
                self.install(install_service=True)
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertFalse((self.root / 'releases' / SHA).exists())
        self.assertFalse((self.config.parent / 'host.env').exists())
        self.assertEqual(list(self.config.parent.glob('host.env.*')), [])
        self.assertFalse(any(args[0] == 'systemctl' for args in self.calls))

    def test_normal_update_does_not_import_ambient_provider_environment(self):
        self.install()
        self.assertFalse((self.config.parent / 'host.env').exists())

    def test_initial_staging_writes_units_without_enabling_or_starting_them(self):
        self.install(install_service=True, restart=False)
        units = Path(self.temporary.name) / '.config/systemd/user'
        self.assertEqual([path.name for path in sorted(units.iterdir())], sorted(updater.UNITS))
        self.assertEqual([args for args in self.calls if args[0] == 'systemctl'],
                         [['systemctl', '--user', 'daemon-reload']])

    def test_direct_install_restarts_for_new_release_before_enabling_timer(self):
        old = self.old_release()
        self.install(install_service=True)
        self.assertEqual((self.root / 'previous').resolve(), old)
        self.assertEqual((self.root / 'current').resolve().name, SHA)
        self.assertTrue(any(args[2:] == ['restart', updater.UNITS[0]] for args in self.calls))
        observation = next(i for i, args in enumerate(self.calls) if args[-1] == STATUS_PATH)
        daemon_enabled = self.calls.index(['systemctl', '--user', 'enable', updater.UNITS[0]])
        timer_enabled = self.calls.index(['systemctl', '--user', 'enable', '--now', updater.UNITS[2]])
        self.assertLess(observation, daemon_enabled)
        self.assertLess(daemon_enabled, timer_enabled)
        active = ['systemctl', '--user', 'is-active', '--quiet', updater.UNITS[0]]
        self.assertEqual(self.calls[observation - 6:observation], [active] * 6)
        self.assertEqual(self.calls[observation + 1], active)

    def test_direct_install_old_server_rolls_back_without_enabling_timer(self):
        old = self.old_release()
        (self.root / 'updater.py').write_bytes(b'old updater')
        self.status_rc = 1
        self.status_output = b'synthetic-sensitive-output'
        with self.assertRaisesRegex(RuntimeError, '^host daemon connection not verified$'):
            self.install(install_service=True)
        self.assertFalse(any('enable' in args for args in self.calls))
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertEqual((self.root / 'updater.py').read_bytes(), b'old updater')
        self.assertFalse((self.root / 'releases' / SHA).exists())

    def test_readiness_rejects_wrong_socket_status_and_sanitizes_failed_transport(self):
        self.install(install_service=True, restart=False)
        correct = dict(self.status)
        cases = [
            ('wrong machine', {'machine_id': 'other-machine'}, None, False),
            ('disconnected', {'connected': False}, None, False),
            ('invalid connected flag', {'connected': 1}, None, False),
            ('reported stale release', {'host_version': OLD_SHA}, None, False),
            ('wrong protocol', {'protocol_version': 2}, None, False),
            ('invalid protocol type', {'protocol_version': True}, None, False),
            ('server mismatch', {'version_mismatch': True}, None, False),
            ('missing mismatch', {}, json.dumps({k: v for k, v in correct.items() if k != 'version_mismatch'}).encode(), False),
            ('oversized', {}, json.dumps(correct).encode() + b' ' * 4096, False),
            ('malformed', {}, b'synthetic-sensitive-output', False),
            ('timed out', {}, None, True),
        ]
        with patch.object(updater, 'command', self.command), patch.object(updater.time, 'sleep'), \
             patch.object(Path, 'home', return_value=Path(self.temporary.name)), \
             patch.object(updater.subprocess, 'run', self.status_command):
            for name, changes, raw, timeout in cases:
                with self.subTest(name=name):
                    self.status = {**correct, **changes}
                    self.status_output = raw
                    self.status_timeout = timeout
                    self.calls.clear()
                    with self.assertRaisesRegex(RuntimeError, '^host daemon connection not verified$') as failure:
                        updater.check_service_ready(self.root / 'current')
                    self.assertIsNone(failure.exception.__cause__)
                    self.assertEqual(sum('is-active' in args for args in self.calls), 6)
                    self.assertEqual(self.calls[-1][-1], STATUS_PATH)

    def test_explicit_install_fails_during_backoff_but_normal_update_defers(self):
        old = self.old_release()
        (self.root / 'failed-releases.json').write_text(json.dumps({SHA: {'retry_at': 4102444800}}))
        for restart in (True, False):
            with self.subTest(restart=restart), self.assertRaisesRegex(RuntimeError, 'activation backoff'):
                self.install(install_service=True, restart=restart)
        self.install()
        self.assertEqual(self.calls, [])
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertFalse((self.root / 'releases' / SHA).exists())

    def test_updater_replacement_failure_rolls_back_without_enabling_timer(self):
        old = self.old_release()
        installed_updater = self.root / 'updater.py'
        installed_updater.write_bytes(b'old updater')
        replace = Path.replace

        def fail_replace(path, target):
            if target == installed_updater:
                raise OSError('updater publication failed')
            return replace(path, target)

        with patch.object(Path, 'replace', fail_replace), self.assertRaisesRegex(OSError, 'publication failed'):
            self.install(install_service=True)
        self.assertEqual((self.root / 'current').resolve(), old)
        self.assertEqual(installed_updater.read_bytes(), b'old updater')
        self.assertFalse(any('enable' in args for args in self.calls))

    def test_timer_enable_failure_leaves_committed_release_for_explicit_install_retry(self):
        old = self.old_release()
        timer_enable = ['systemctl', '--user', 'enable', '--now', updater.UNITS[2]]
        record_command = self.command

        def fail_enable(args):
            record_command(args)
            if args == timer_enable:
                raise RuntimeError('timer enable failed')

        self.command = fail_enable
        with self.assertRaisesRegex(RuntimeError, 'timer enable failed'):
            self.install(install_service=True)
        target = (self.root / 'current').resolve()
        self.assertEqual(target.name, SHA)
        self.assertEqual((self.root / 'previous').resolve(), old)
        self.assertFalse((target / '.installing').exists())
        self.assertFalse((self.root / 'failed-releases.json').exists())
        self.assertEqual((self.root / 'updater.py').read_bytes(), self.files['hub2-self-update.py'])
        self.command = record_command
        self.calls.clear()
        self.install(install_service=True)
        self.assertIn(['systemctl', '--user', 'restart', updater.UNITS[0]], self.calls)
        self.assertIn(timer_enable, self.calls)
        self.assertEqual((self.root / 'current').resolve(), target)

    def test_same_version_rebuild_repairs_only_shared_artifacts_without_restart(self):
        self.install(install_service=True, restart=False)
        target = (self.root / 'current').resolve()
        old_receipt = (target / 'release.json').read_bytes()
        old_cli = (target / 'hub.pyz').read_bytes()
        old_enroller = (target / 'hub2-enroll.py').read_bytes()
        rebuilt = {'hub.pyz': b'rebuilt cli', HOST: b'rebuilt host wheel',
                   'manifest.json': json.dumps({'built_at_ms': 123456}).encode()}
        for name, data in rebuilt.items():
            self.files[name] = data
            self.manifest['artifacts'][name] = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        units = Path(self.temporary.name) / '.config/systemd/user'
        (units / updater.UNITS[1]).write_text('outdated')
        (units / updater.UNITS[2]).unlink()
        (self.root / 'updater.py').unlink()
        self.calls.clear()
        self.downloads.clear()
        self.install(install_service=True, restart=False)
        self.assertCountEqual(self.downloads, ['host-release.json', 'hub2-self-update.py', *updater.UNITS[1:]])
        self.assertEqual((target / 'release.json').read_bytes(), old_receipt)
        self.assertEqual((target / 'hub.pyz').read_bytes(), old_cli)
        self.assertEqual((target / 'hub2-enroll.py').read_bytes(), old_enroller)
        for name in updater.UNITS:
            self.assertEqual((units / name).read_bytes(), self.files[name])
        self.assertEqual((self.root / 'updater.py').read_bytes(), self.files['hub2-self-update.py'])
        self.assertEqual((self.root / 'current').resolve(), target)
        self.assertFalse((target / '.installing').exists())
        self.assertFalse(any('venv' in args for args in self.calls))
        self.assertEqual([args for args in self.calls if args[0] == 'systemctl'],
                         [['systemctl', '--user', 'daemon-reload']])
        self.calls.clear()
        self.install()
        self.assertEqual(self.calls, [])

    def test_same_version_stage_rejects_changed_protocol_or_service_records(self):
        self.install(install_service=True, restart=False)
        units = Path(self.temporary.name) / '.config/systemd/user'
        timer = units / updater.UNITS[2]
        timer.unlink()
        artifacts = self.manifest['artifacts']
        cases = [
            ('protocol', {'protocol_version': 2}, 'release identity'),
            ('timer checksum', {'artifacts': {**artifacts, updater.UNITS[2]: {**artifacts[updater.UNITS[2]], 'sha256': '0' * 64}}},
             'service artifact mismatch'),
            ('updater size', {'artifacts': {**artifacts, 'hub2-self-update.py': {**artifacts['hub2-self-update.py'], 'size': 1}}},
             'service artifact mismatch'),
        ]
        for case, fields, error in cases:
            with self.subTest(case=case), patch.dict(self.manifest, fields):
                self.calls.clear()
                self.downloads.clear()
                with self.assertRaisesRegex(ValueError, error):
                    self.install(install_service=True, restart=False)
                self.assertEqual(self.downloads, ['host-release.json'])
                self.assertEqual(self.calls, [])
                self.assertFalse(timer.exists())

    def test_same_version_stage_requires_a_valid_local_receipt_and_enroller(self):
        self.install(install_service=True, restart=False)
        target = (self.root / 'current').resolve()
        receipt = target / 'release.json'
        original = receipt.read_bytes()
        receipt.write_text('[]')
        with self.assertRaisesRegex(ValueError, 'unsupported release protocol'):
            self.install(install_service=True, restart=False)
        receipt.write_bytes(original)
        (target / 'hub2-enroll.py').write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, 'installed release artifact mismatch: hub2-enroll.py'):
            self.install(install_service=True, restart=False)

    def test_same_version_stage_refuses_corrupt_repair_before_writing_units(self):
        self.install(install_service=True, restart=False)
        units = Path(self.temporary.name) / '.config/systemd/user'
        (units / updater.UNITS[1]).unlink()
        (units / updater.UNITS[2]).unlink()
        self.files[updater.UNITS[2]] = b'corrupt'
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            self.install(install_service=True, restart=False)
        self.assertFalse((units / updater.UNITS[1]).exists())
        self.assertFalse((units / updater.UNITS[2]).exists())
        self.assertFalse(any(args[0] == 'systemctl' for args in self.calls))

    def test_shell_starts_only_daemon_until_enrollment_and_readiness_succeed(self):
        for scenario in ('fresh', 'enroll', 'start', 'is-active', 'routing', 'enrolled', 'backoff'):
            enrolled = scenario in ('enrolled', 'backoff')
            failure = '' if scenario in ('fresh', 'enrolled') else scenario
            with self.subTest(scenario=scenario):
                home = Path(self.temporary.name) / ('shell-' + scenario)
                root = home / '.local/share/agent-hub2'
                current = root / 'current'
                target = root / 'releases' / SHA
                current_bin = target / 'bin'
                current_bin.mkdir(parents=True)
                (target / 'release.json').write_text(json.dumps(self.manifest))
                current.symlink_to(target)
                fake_bin = home / 'fake-bin'
                fake_bin.mkdir()
                log = home / 'commands.jsonl'
                config = home / '.config/agent-hub/v2.json'
                enrollment = json.dumps({'machine_id': MACHINE, 'token': 'synthetic-enrollment-token'})
                if enrolled:
                    config.parent.mkdir(parents=True)
                    config.write_text(enrollment)
                shim = home / 'downloaded.py'
                shim.write_text(f"""
import json, os, runpy, sys, time
if '--no-restart' in sys.argv:
    assert '--install-service' in sys.argv
    with open(os.environ['INSTALL_TEST_LOG'], 'a') as stream:
        stream.write(json.dumps(['stage']) + '\\n')
elif '--install-service' in sys.argv:
    with open(os.environ['INSTALL_TEST_LOG'], 'a') as stream:
        stream.write(json.dumps(['restart-install']) + '\\n')
    if os.environ['INSTALL_TEST_FAIL'] == 'backoff':
        sys.exit(1)
else:
    time.sleep = lambda _: None
    runpy.run_path({str(REPO / 'scripts/hub2-self-update.py')!r}, run_name='__main__')
""")
                stub = fake_bin / 'stub'
                stub.write_text(f"""#!{sys.executable}
import json, os, shutil, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
if name == 'python' and args[:3] != ['-I', '-m', 'agent_hub_host.cli']:
    os.execv(sys.executable, [sys.executable, *args])  # only the host CLI module is stubbed
config = Path(os.environ['INSTALL_TEST_CONFIG'])
if name == 'curl':
    shutil.copyfile(os.environ['INSTALL_TEST_UPDATER'], args[args.index('-o')+1])
    sys.exit(0)
if name == 'hub2-enroll.py':
    event = 'enroll'
elif name == 'systemctl':
    event = args[1]
    assert args[0] == '--user'
elif name == 'python' and args[:3] == ['-I', '-m', 'agent_hub_host.cli']:
    args = args[3:]
    assert args == ['--config', str(config), 'request', 'GET', {STATUS_PATH!r}]
    assert os.environ['HUB2_TOKEN'] == 'synthetic-override-token'
    event = 'routing'
else:
    assert name == 'loginctl'
    event = name
with open(os.environ['INSTALL_TEST_LOG'], 'a') as stream:
    stream.write(json.dumps([event, *args]) + '\\n')
if event == os.environ['INSTALL_TEST_FAIL']:
    sys.exit(1)
if event == 'enroll':
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text({enrollment!r})
elif event in ('start', 'routing', 'enable'):
    assert config.is_file()
    if event == 'routing':
        print({json.dumps(self.status)!r})
""")
                stub.chmod(0o700)
                for name in ('curl', 'loginctl', 'systemctl'):
                    (fake_bin / name).symlink_to(stub)
                (fake_bin / 'python3').symlink_to(sys.executable)
                (current_bin / 'python').symlink_to(stub)
                (current / 'hub2-enroll.py').symlink_to(stub)
                install_env = self.owned_child_env(HOME=str(home),
                                                   PATH=str(fake_bin) + ':/usr/bin:/bin',
                                                   HUB_BASE_URL='https://example.invalid',
                                                   HUB_WORKSPACE_ROOT=str(home),
                                                   HUB2_TOKEN='synthetic-override-token',
                                                   INSTALL_TEST_LOG=str(log), INSTALL_TEST_CONFIG=str(config),
                                                   INSTALL_TEST_UPDATER=str(shim), INSTALL_TEST_FAIL=failure)
                install_argv = ['/bin/sh', str(REPO / 'deploy/host-install.sh')]
                with self.owned_child(lambda argv, kwargs: argv == install_argv
                                      and kwargs.get('cwd') is None
                                      and kwargs.get('env') == install_env
                                      and sorted(kwargs) == ['capture_output', 'env', 'text', 'timeout']):
                    result = subprocess.run(install_argv, env=install_env,
                                            capture_output=True, text=True, timeout=10)
                commands = [json.loads(line) for line in log.read_text().splitlines()]
                events = [args[0] for args in commands]
                if enrolled:
                    self.assertEqual(events, ['loginctl', 'restart-install'])
                    self.assertEqual(result.returncode, 1 if failure else 0, result.stderr)
                    self.assertEqual('installed and enrolled' in result.stdout, not failure)
                elif failure:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(events[-1], failure)
                    self.assertNotIn('enable', events)
                    self.assertFalse(any(updater.UNITS[2] in args for args in commands))
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(events, ['stage', 'enroll', 'loginctl', 'start', *(['is-active'] * 6),
                                              'routing', 'is-active', 'enable', 'enable'])
                    self.assertEqual(commands[3], ['start', '--user', 'start', updater.UNITS[0]])
                    self.assertEqual(commands[-1], ['enable', '--user', 'enable', '--now', updater.UNITS[2]])

    def test_reject_unsafe_manifest_and_protocol_before_install(self):
        for key, value in [('protocol_version', 0), ('host_version', '../invalid'), ('python_requires', '>=99.0')]:
            with self.subTest(key=key):
                invalid = dict(self.manifest, **{key: value})
                with self.assertRaises(ValueError):
                    updater.validate_manifest(invalid)
        self.manifest['artifacts']['../escape'] = self.manifest['artifacts'][HOST]
        with self.assertRaisesRegex(ValueError, 'unsafe artifact'):
            self.install()
        self.assertEqual(self.calls, [])

    def test_existing_unmanaged_environment_is_preserved(self):
        target = self.root / 'releases' / SHA
        target.mkdir(parents=True)
        marker = target / 'owner-data'
        marker.write_text('preserve')
        with self.assertRaisesRegex(ValueError, 'explicit recovery'):
            self.install()
        self.assertEqual(marker.read_text(), 'preserve')

    def test_export_excludes_tracked_build_output(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode='w') as archive:
            for name in ['hub2/host/agent_hub_host/client.py', 'hub2/host/build/lib/agent_hub_host/client.py']:
                entry = tarfile.TarInfo(name)
                entry.size = 5
                archive.addfile(entry, io.BytesIO(b'code\n'))
        with patch.object(builder, 'run', side_effect=[(SHA + '\n').encode(), data.getvalue()]):
            builder.export_source(REPO, 'HEAD', Path(self.temporary.name))
        self.assertTrue((Path(self.temporary.name) / 'hub2/host/agent_hub_host/client.py').is_file())
        self.assertFalse((Path(self.temporary.name) / 'hub2/host/build').exists())

    def test_enrollment_defaults_to_central_credentials_and_never_replaces_config(self):
        class Client:
            base_url = 'https://example.invalid'

            def request(self, method, path, body):
                self.request_body = body
                return {'machine_id': 'test-machine', 'token': 'test-secret'}

        client = Client()
        saved = []
        path = Path(self.temporary.name) / 'config.json'
        result = enrollment.enroll(client, lambda path, data: saved.append(data), path,
                                   [self.temporary.name], ['codex'], 'test-code')
        self.assertEqual(result, 'test-machine')
        self.assertEqual(saved[0]['launcher'], {'private_provider_homes': True})
        self.assertEqual(saved[0]['token'], 'test-secret')
        self.assertNotIn('code', saved[0])
        path.write_text('existing')
        with self.assertRaisesRegex(ValueError, 'refusing to replace'):
            enrollment.enroll(client, lambda *_: self.fail('must not save'), path,
                              [self.temporary.name], ['codex'], 'new-code')

    def _expected_tuple(self, manifest=None):
        m = manifest or self.manifest
        return {
            'expected_host_version': m['host_version'],
            'expected_cli_sha256': m['artifacts']['hub.pyz']['sha256'],
            'expected_updater_sha256': m['artifacts']['hub2-self-update.py']['sha256'],
        }

    def test_expected_tuple_rejects_partial_malformed_and_check_service_ready_cli(self):
        tuple_kwargs = self._expected_tuple()
        for bad_kwargs in (
            {'expected_host_version': SHA},
            {'expected_host_version': SHA, 'expected_cli_sha256': tuple_kwargs['expected_cli_sha256']},
            {**tuple_kwargs, 'expected_host_version': 'not-40-hex'},
            {**tuple_kwargs, 'expected_cli_sha256': 'A' * 64},
            {**tuple_kwargs, 'expected_updater_sha256': '0' * 63},
        ):
            with self.assertRaises(ValueError):
                updater.install('https://example.invalid', self.root, **bad_kwargs)
        self.assertFalse(self.root.exists())

        for bad_argv in (
            ['--root', str(self.root), '--expected-host-version', SHA],
            ['--root', str(self.root), '--check-service-ready',
             '--expected-host-version', SHA,
             '--expected-cli-sha256', tuple_kwargs['expected_cli_sha256'],
             '--expected-updater-sha256', tuple_kwargs['expected_updater_sha256']],
            ['--root', str(self.root),
             '--expected-host-version', 'bad',
             '--expected-cli-sha256', tuple_kwargs['expected_cli_sha256'],
             '--expected-updater-sha256', tuple_kwargs['expected_updater_sha256']],
        ):
            with patch.object(sys, 'argv', ['hub2-self-update.py', *bad_argv]), \
                 patch.object(updater, 'install', side_effect=AssertionError('install must not run')):
                with self.assertRaises(SystemExit) as exc:
                    updater.main()
                self.assertEqual(exc.exception.code, 2)

        seen = []
        def fake_install(base_url, root, **kwargs):
            seen.append((base_url, root, kwargs, updater.HANDOFF_ARGV.get()))
        sentinel = ['--sentinel']
        prior_token = updater.HANDOFF_ARGV.set(sentinel)
        try:
            with patch.object(updater, 'install', fake_install), \
                 patch.object(Path, 'home', return_value=Path(self.temporary.name)), \
                 patch.dict(os.environ, {}, clear=False):
                os.environ.pop('TRIGGER_UNIT', None)
                with patch.object(sys, 'argv', ['hub2-self-update.py', '--base-url', 'https://example.invalid', '--root', str(self.root)]):
                    self.assertEqual(updater.main(), 0)
                self.assertEqual(updater.HANDOFF_ARGV.get(), sentinel)
                with patch.object(sys, 'argv', ['hub2-self-update.py', '--no-handoff', '--base-url', 'https://example.invalid', '--root', str(self.root)]):
                    self.assertEqual(updater.main(), 0)
                self.assertEqual(updater.HANDOFF_ARGV.get(), sentinel)
                with patch.object(sys, 'argv', [
                    'hub2-self-update.py', '--base-url', 'https://example.invalid', '--root', str(self.root),
                    '--expected-host-version', tuple_kwargs['expected_host_version'],
                    '--expected-cli-sha256', tuple_kwargs['expected_cli_sha256'],
                    '--expected-updater-sha256', tuple_kwargs['expected_updater_sha256'],
                ]):
                    self.assertEqual(updater.main(), 0)
                self.assertEqual(updater.HANDOFF_ARGV.get(), sentinel)
            with patch.object(updater, 'install', side_effect=RuntimeError('boom')), \
                 patch.object(Path, 'home', return_value=Path(self.temporary.name)), \
                 patch.dict(os.environ, {}, clear=False), \
                 contextlib.redirect_stderr(io.StringIO()):
                os.environ.pop('TRIGGER_UNIT', None)
                with patch.object(sys, 'argv', ['hub2-self-update.py', '--base-url', 'https://example.invalid', '--root', str(self.root)]):
                    self.assertEqual(updater.main(), 1)
                self.assertEqual(updater.HANDOFF_ARGV.get(), sentinel)
        finally:
            updater.HANDOFF_ARGV.reset(prior_token)
        self.assertEqual(seen, [
            ('https://example.invalid', self.root.resolve(),
             {'install_service': False, 'restart': True, 'scheduled': False},
             ['--base-url', 'https://example.invalid', '--root', str(self.root)]),
            ('https://example.invalid', self.root.resolve(),
             {'install_service': False, 'restart': True, 'scheduled': False},
             None),
            ('https://example.invalid', self.root.resolve(),
             {'install_service': False, 'restart': True, 'scheduled': False, **tuple_kwargs},
             None),
        ])

    def test_expected_tuple_under_lock_rejects_changed_manifest_busy_lock_and_backoff(self):
        old = self.old_release()
        tuple_kwargs = self._expected_tuple()

        # 1. Busy update.lock refuses without mutating state.
        lock_path = self.root / 'update.lock'
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, lock_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with self.assertRaises(BlockingIOError):
                self.install(install_service=True, **tuple_kwargs)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        self.assertEqual(self.calls, [])

        # 2. Published manifest changed to OLD_SHA (with internally valid wheel metadata)
        # under update.lock refuses BEFORE recover() or any mutation.
        original_manifest = self.manifest
        other_manifest = json.loads(json.dumps(original_manifest))
        other_manifest['host_version'] = OLD_SHA
        other_manifest['artifacts'][f'agent_hub_host_v2-2.0.0+g{OLD_SHA}-py3-none-any.whl'] = (
            other_manifest['artifacts'].pop(HOST)
        )
        self.manifest = other_manifest
        self.downloads.clear()
        pending_argv = ['--install-service']
        handoff_token = updater.HANDOFF_ARGV.set(pending_argv)
        try:
            with patch.object(updater, 'maybe_handoff_updater', side_effect=AssertionError('handoff must not run for expected tuple')), \
                 patch.object(updater, 'recover', side_effect=AssertionError('recover must not run on mismatch')):
                with self.assertRaisesRegex(ValueError, 'expected candidate tuple'):
                    self.install(install_service=True, **tuple_kwargs)
            self.assertEqual(updater.HANDOFF_ARGV.get(), pending_argv)
        finally:
            updater.HANDOFF_ARGV.reset(handoff_token)
        self.assertEqual(self.downloads, ['host-release.json'])
        self.assertEqual((self.root / 'current').resolve(), old.resolve())
        self.assertFalse((self.root / 'releases' / SHA).exists())
        self.assertEqual(self.calls, [])

        # 3. Failed-version backoff is preserved and refuses install_service=True even with matching tuple.
        self.manifest = original_manifest
        updater.write_json(self.root / 'failed-releases.json', {SHA: {'count': 1, 'retry_at': time.time() + 300}})
        with self.assertRaisesRegex(RuntimeError, 'activation backoff'):
            self.install(install_service=True, **tuple_kwargs)
        self.assertTrue((self.root / 'failed-releases.json').exists())

    def test_expected_tuple_fresh_install_and_publication_change_during_prepare(self):
        old = self.old_release()
        tuple_kwargs = self._expected_tuple()
        a00d = '/home/alexey/.local/lib/aplexer/releases/a00d4bdb69d1793a9738b893fb21f0001d4acc12/bin/aplexer'
        dropin_dir = Path(self.temporary.name) / '.config/systemd/user/agent-hub2-host.service.d'
        dropin_dir.mkdir(parents=True, exist_ok=True)
        dropin = dropin_dir / 'override.conf'
        dropin.write_text(
            f'[Service]\n# keep-comment\nEnvironment=FOO=bar\nExecStart=\n'
            f'ExecStart=%h/host/current/bin/hub2 serve --aplexer {a00d}\n'
        )
        foreign_dropin = dropin_dir / 'foreign.conf'
        foreign_text = f'[Service]\nExecStart=\nExecStart=/foreign/root/current/bin/hub2 serve --aplexer {a00d}\n'
        foreign_dropin.write_text(foreign_text)

        # 1. Publication change during prepare_environment refuses before switching current or restarting.
        real_prepare = updater.prepare_environment
        def mutating_prepare(target, download, manifest, host_wheel):
            real_prepare(target, download, manifest, host_wheel)
            self.manifest = {**self.manifest, 'host_version': OLD_SHA}

        with patch.object(updater, 'prepare_environment', side_effect=mutating_prepare):
            with self.assertRaisesRegex(ValueError, 'changed during candidate preparation'):
                self.install(install_service=True, **tuple_kwargs)
        self.assertEqual((self.root / 'current').resolve(), old.resolve())
        self.assertFalse((self.root / 'releases' / SHA).exists())
        self.assertFalse(any('restart' in call for call in self.calls))

        # 2. Matching expected tuple succeeds through real install() and align_host_dropins preserves a00d + hub-only.
        self.manifest = {**self.manifest, 'host_version': SHA}
        (self.root / 'failed-releases.json').unlink(missing_ok=True)
        self.install(install_service=True, **tuple_kwargs)
        self.assertEqual((self.root / 'current').resolve(), (self.root / 'releases' / SHA).resolve())
        self.assertEqual((self.root / 'updater.py').read_bytes(), self.files['hub2-self-update.py'])
        self.assertEqual(
            dropin.read_text(),
            f'[Service]\n# keep-comment\nEnvironment=FOO=bar\nExecStart=\n'
            f'ExecStart=%h/host/current/bin/hub host serve --aplexer {a00d}\n',
        )
        self.assertEqual(foreign_dropin.read_text(), foreign_text)

    def test_expected_tuple_same_version_refuses_mismatch_and_repairs_matching(self):
        tuple_kwargs = self._expected_tuple()
        self.install(install_service=True, restart=False)
        target = (self.root / 'current').resolve()
        original_receipt = (target / 'release.json').read_bytes()

        # Same-version: receipt with mismatched CLI sha refuses before repair/restart.
        bad_receipt = json.loads(original_receipt.decode())
        bad_receipt['artifacts']['hub.pyz']['sha256'] = 'f' * 64
        updater.write_json(target / 'release.json', bad_receipt)
        with patch.object(updater, 'repair_alert_client', side_effect=AssertionError('must not repair')):
            with self.assertRaisesRegex(ValueError, 'installed release artifacts'):
                self.install(install_service=True, **tuple_kwargs)

        # Same-version: matching complete installed release + receipt succeeds through repair_service_files.
        (target / 'release.json').write_bytes(original_receipt)
        self.calls.clear()
        self.install(install_service=True, **tuple_kwargs)
        self.assertIn(['systemctl', '--user', 'daemon-reload'], self.calls)
        self.assertIn(['systemctl', '--user', 'restart', updater.UNITS[0]], self.calls)
        self.assertEqual((self.root / 'current').resolve(), target)

    def test_expected_tuple_retained_activation_refuses_changed_publication_and_rolls_back(self):
        tuple_kwargs = self._expected_tuple()
        self.install(install_service=True, restart=False)
        old = self.root / 'releases' / OLD_SHA
        old.mkdir(parents=True)
        updater.link(old, self.root / 'current')
        self.status = {'machine_id': MACHINE, 'connected': True, 'host_version': SHA,
                       'protocol_version': 1, 'version_mismatch': False}
        fetch_count = 0
        def changing_fetch(url, limit):
            nonlocal fetch_count
            if url.endswith('/host-release.json'):
                fetch_count += 1
                if fetch_count >= 2:
                    return json.dumps({**self.manifest, 'host_version': OLD_SHA}).encode()
            return self.fetch(url, limit)

        with patch.object(updater, 'fetch', changing_fetch), \
             patch.object(updater, 'command', self.command), \
             patch.object(updater.time, 'sleep'), \
             patch.object(Path, 'home', return_value=Path(self.temporary.name)), \
             patch.object(updater.subprocess, 'run', self.status_command):
            with self.assertRaisesRegex(ValueError, 'changed during retained activation'):
                updater.install('https://example.invalid', self.root, install_service=True, **tuple_kwargs)
        self.assertEqual(fetch_count, 2)
        self.assertEqual((self.root / 'current').resolve(), old.resolve())
        self.assertFalse((self.root / 'releases' / ('.retained-' + SHA)).exists())
        self.assertFalse((self.root / 'retained-switch.json').exists())
        self.assertTrue((self.root / 'releases' / SHA / 'release.json').is_file())

    def test_legacy_updater_fails_on_unified_candidate_and_trusted_bootstrap_converges(self):
        # 1. Provenance-pinned historical prepare_environment (exact 21 lines, AST-loaded in isolation).
        self.assertEqual(
            hashlib.sha256(LEGACY_PREPARE_ENVIRONMENT_SOURCE.encode()).hexdigest(),
            '826e90bb8174e3bf1c8894535cc1b7fcdf93799cdb03196200617393a702e3b4',
        )
        parsed = ast.parse(LEGACY_PREPARE_ENVIRONMENT_SOURCE)
        self.assertEqual(len(parsed.body), 1)
        self.assertIsInstance(parsed.body[0], ast.FunctionDef)
        self.assertEqual(parsed.body[0].name, 'prepare_environment')

        # 2. One shared synthetic unified-only candidate (declares hub only, no bin/hub2).
        wheel_buf = io.BytesIO()
        with zipfile.ZipFile(wheel_buf, 'w') as archive:
            archive.writestr('agent_hub_host/__init__.py', '')
            archive.writestr('agent_hub_host/_release.py',
                             f'HOST_VERSION = {SHA!r}\nPROTOCOL_VERSION = 1\n')
            archive.writestr('agent_hub_host/client.py',
                             'class Client:\n    def heartbeat(self, native_sessions=None):\n        pass\n')
            archive.writestr('agent_hub_host/cli.py',
                             'import sys\n'
                             'def main(argv=None):\n'
                             '    args = sys.argv[1:] if argv is None else argv\n'
                             '    if "--help" in args:\n'
                             '        print("unified candidate module cli help")\n'
                             '        return 0\n'
                             '    return 2\n'
                             'if __name__ == "__main__":\n'
                             '    raise SystemExit(main())\n')
            archive.writestr('agent_hub_host-2.0.0.dist-info/entry_points.txt',
                             '[console_scripts]\nhub = agent_hub_host.wheel_cli:main\n')
        self.files[HOST] = wheel_buf.getvalue()

        pyz_src = Path(self.temporary.name) / 'pyz-src'
        pyz_src.mkdir()
        (pyz_src / '__main__.py').write_text(
            'import sys\n'
            'if any(flag in sys.argv[1:] for flag in ("--help", "--version")):\n'
            '    print("unified hub.pyz help")\n'
            '    raise SystemExit(0)\n'
            'raise SystemExit(2)\n'
        )
        pyz_buf = io.BytesIO()
        zipapp.create_archive(pyz_src, pyz_buf)
        self.files['hub.pyz'] = pyz_buf.getvalue()
        self.files['hub2-self-update.py'] = Path(updater.__file__).read_bytes()
        self.manifest = {
            **self.manifest,
            'artifacts': {
                name: {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
                for name, data in self.files.items()
            },
        }
        download = Path(self.temporary.name) / 'staged-unified-download'
        download.mkdir()
        for name, data in self.files.items():
            (download / name).write_bytes(data)

        executed_probes = []
        add_legacy_entry = False
        guarded_run = subprocess.run

        def run_bounded_command(args):
            args = [str(value) for value in args]
            if not self._owned_command_executable(args[0]):
                raise AssertionError('install test reached an external command: ' + args[0])
            self.calls.append(args)
            if args[0] == 'systemctl':
                return
            if args[:3] == [sys.executable, '-m', 'venv']:
                self.release_venv(Path(args[3]))
                return
            if (len(args) >= 6 and Path(args[0]).name == 'python'
                    and args[1:5] == ['-m', 'pip', 'install', '--no-index']):
                target_env = Path(args[0]).parents[1]
                site = target_env / 'lib' / ('python%d.%d' % sys.version_info[:2]) / 'site-packages'
                with zipfile.ZipFile(args[-1]) as zf:
                    zf.extractall(site)
                if add_legacy_entry:
                    launcher = target_env / 'bin/hub2'
                    launcher_bytes = self.pip_launcher_bytes(target_env)
                    shebang = launcher_bytes.splitlines(True)[0].rstrip(b'\n')
                    if len(shebang) <= 255:
                        launcher.write_bytes(launcher_bytes)
                    else:
                        launcher.write_text(
                            f'#!/bin/sh\nexec "{target_env / "bin/python"}" -I -m agent_hub_host.cli "$@"\n'
                        )
                    launcher.chmod(0o755)
                return
            probe_env = self.owned_child_env()
            with self.owned_child(
                lambda argv, kwargs: argv == args
                and kwargs.get('cwd') == str(self.temporary.name)
                and kwargs.get('env') == probe_env
                and sorted(kwargs) == ['capture_output', 'cwd', 'env', 'text', 'timeout']
            ):
                result = guarded_run(
                    args, cwd=str(self.temporary.name), env=probe_env,
                    capture_output=True, text=True, timeout=30,
                )
            executed_probes.append((args, result.returncode, result.stdout))
            if result.returncode:
                raise RuntimeError('release command failed: ' + Path(args[0]).name)

        legacy_ns = {
            'Path': Path,
            'shutil': shutil,
            'sys': sys,
            'command': run_bounded_command,
            'check_host_package': updater.check_host_package,
            'ALERT_CLIENT': updater.ALERT_CLIENT,
            'retain_alert_client': updater.retain_alert_client,
            'install_operator_cli': updater.install_operator_cli,
            'write_json': updater.write_json,
        }
        exec(compile(parsed, '<legacy-prepare-environment-0cc47732>', 'exec'), legacy_ns)
        legacy_prepare = legacy_ns['prepare_environment']

        # Pre-existing legacy host state, units, drop-in, and native a00d selector.
        old = self.old_release()
        legacy_updater_bytes = b'# legacy pre-unification updater\n'
        (self.root / 'updater.py').write_bytes(legacy_updater_bytes)
        units_dir = Path(self.temporary.name) / '.config/systemd/user'
        units_dir.mkdir(parents=True, exist_ok=True)
        legacy_unit_bytes = f'[Service]\nExecStart=%h/host/current/bin/hub2 serve\n'.encode()
        (units_dir / updater.UNITS[0]).write_bytes(legacy_unit_bytes)
        a00d = '/home/alexey/.local/lib/aplexer/releases/a00d4bdb69d1793a9738b893fb21f0001d4acc12/bin/aplexer'
        dropin_dir = units_dir / (updater.UNITS[0] + '.d')
        dropin_dir.mkdir(parents=True, exist_ok=True)
        dropin = dropin_dir / 'override.conf'
        legacy_dropin_text = (
            f'[Service]\n# keep-comment\nEnvironment=FOO=bar\nExecStart=\n'
            f'ExecStart=%h/host/current/bin/hub2 serve --aplexer {a00d}\n'
        )
        dropin.write_text(legacy_dropin_text)
        foreign_dropin = dropin_dir / 'foreign.conf'
        foreign_text = f'[Service]\nExecStart=\nExecStart=/foreign/root/current/bin/hub2 serve --aplexer {a00d}\n'
        foreign_dropin.write_text(foreign_text)
        native_selector = Path(os.environ['APLEXER_CONFIG'])
        native_selector.parent.mkdir(parents=True, exist_ok=True)
        native_selector_text = 'default_profile = "native-a00d"\n'
        native_selector.write_text(native_selector_text)

        # Arm 1 (Negative): legacy prepare_environment passes check_host_package then fails on missing bin/hub2.
        legacy_target = self.root / 'releases' / ('legacy-' + SHA)
        with patch.object(updater, 'command', run_bounded_command), \
             patch.object(Path, 'home', return_value=Path(self.temporary.name)):
            with self.assertRaises(FileNotFoundError) as exc_info:
                legacy_prepare(legacy_target, download, self.manifest, HOST)
        self.assertEqual(Path(exc_info.exception.filename), legacy_target / 'bin/hub2')
        self.assertTrue(any(
            argv[0] == str(legacy_target / 'bin/python') and argv[1:3] == ['-I', '-c'] and rc == 0
            for argv, rc, _ in executed_probes
        ))
        self.assertFalse((legacy_target / 'release.json').exists())
        self.assertEqual((self.root / 'current').resolve(), old.resolve())
        self.assertEqual((self.root / 'updater.py').read_bytes(), legacy_updater_bytes)
        self.assertEqual((units_dir / updater.UNITS[0]).read_bytes(), legacy_unit_bytes)
        self.assertEqual(dropin.read_text(), legacy_dropin_text)
        self.assertEqual(native_selector.read_text(), native_selector_text)

        # Arm 2 (Positive legacy-entry control): adding owned bin/hub2 makes the exact legacy function succeed.
        executed_probes.clear()
        add_legacy_entry = True
        control_target = self.root / 'releases' / ('control-' + SHA)
        with patch.object(updater, 'command', run_bounded_command), \
             patch.object(Path, 'home', return_value=Path(self.temporary.name)):
            legacy_prepare(control_target, download, self.manifest, HOST)
        self.assertTrue((control_target / 'release.json').is_file())
        self.assertTrue(any(
            argv == [str(control_target / 'bin/hub2'), '--help']
            and rc == 0 and 'unified candidate module cli help' in out
            for argv, rc, out in executed_probes
        ))

        # Arm 3 (Fail-closed negative): mismatched expected candidate tuple cannot reach preparation/convergence.
        add_legacy_entry = False
        executed_probes.clear()
        tuple_kwargs = self._expected_tuple()
        bad_tuple = {**tuple_kwargs, 'expected_cli_sha256': '0' * 64}
        with patch.object(updater, 'fetch', self.fetch), \
             patch.object(updater, 'command', run_bounded_command), \
             patch.object(updater.time, 'sleep'), \
             patch.object(Path, 'home', return_value=Path(self.temporary.name)), \
             patch.object(updater.subprocess, 'run', self.status_command):
            with self.assertRaisesRegex(ValueError, 'expected candidate tuple'):
                updater.install('https://example.invalid', self.root, install_service=True, **bad_tuple)
        self.assertEqual(executed_probes, [])
        self.assertEqual((self.root / 'current').resolve(), old.resolve())
        self.assertEqual((self.root / 'updater.py').read_bytes(), legacy_updater_bytes)
        self.assertEqual(dropin.read_text(), legacy_dropin_text)
        self.assertEqual(native_selector.read_text(), native_selector_text)

        # Arm 4 (Convergence): current trusted bootstrap (expected_tuple + update_lock) succeeds on same candidate.
        target = self.root / 'releases' / SHA
        with patch.object(updater, 'fetch', self.fetch), \
             patch.object(updater, 'command', run_bounded_command), \
             patch.object(updater.time, 'sleep'), \
             patch.object(Path, 'home', return_value=Path(self.temporary.name)), \
             patch.object(updater.subprocess, 'run', self.status_command), \
             patch.object(updater, 'update_lock', wraps=updater.update_lock) as lock_spy, \
             patch.object(updater, 'validate_expected_manifest', wraps=updater.validate_expected_manifest) as manifest_spy:
            updater.install('https://example.invalid', self.root, install_service=True, **tuple_kwargs)
        self.assertTrue(lock_spy.called)
        self.assertTrue(manifest_spy.called)
        self.assertTrue(any(
            argv == [str(target / 'bin/python'), '-I', '-m', 'agent_hub_host.cli', '--help']
            and rc == 0 and 'unified candidate module cli help' in out
            for argv, rc, out in executed_probes
        ))
        self.assertFalse((target / 'bin/hub2').exists())
        self.assertEqual(os.readlink(target / 'bin/hub'), '../hub.pyz')
        self.assertEqual((self.root / 'current').resolve(), target.resolve())
        self.assertEqual((self.root / 'previous').resolve(), old.resolve())
        self.assertEqual((self.root / 'updater.py').read_bytes(), self.files['hub2-self-update.py'])
        self.assertEqual(
            dropin.read_text(),
            f'[Service]\n# keep-comment\nEnvironment=FOO=bar\nExecStart=\n'
            f'ExecStart=%h/host/current/bin/hub host serve --aplexer {a00d}\n',
        )
        self.assertEqual(foreign_dropin.read_text(), foreign_text)
        self.assertEqual(native_selector.read_text(), native_selector_text)


def publication_bundle(directory, commit, label='fixture'):
    directory.mkdir()
    wheel = f'agent_hub_host_v2-2.0.0+g{commit}-py3-none-any.whl'
    with zipfile.ZipFile(directory / wheel, 'w') as archive:
        archive.writestr('agent_hub_host/_release.py', f'HOST_VERSION = {commit!r}\nPROTOCOL_VERSION = 1\n')
    with zipfile.ZipFile(directory / 'hub.pyz', 'w') as archive:
        archive.writestr('agent_hub_host/_build_info.py', f'BUILD_COMMIT = "{commit}"\n')
        archive.writestr('__main__.py', 'raise RuntimeError("staged code must never run")\n')
    cli_digest = hashlib.sha256((directory / 'hub.pyz').read_bytes()).hexdigest()
    (directory / 'hub.pyz.sha256').write_text(f'{cli_digest}  hub.pyz\n')
    (directory / 'manifest.json').write_text(json.dumps(
        {'built_from_commit': commit, 'sha256': cli_digest, 'build_label': label}))
    for name in ('websockets-16.0-py3-none-any.whl', 'host-install.sh', 'hub2-enroll.py', 'hub_alert_client.py',
                 *builder.CLI_INSTALLERS, *builder.UNITS):
        (directory / name).write_text('artifact:' + name)
    (directory / 'hub2-self-update.py').write_text('raise RuntimeError("staged updater must never run")\n')
    records = {}
    for path in directory.iterdir():
        data = path.read_bytes()
        records[path.name] = {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
    manifest = {'host_version': commit, 'protocol_version': 1, 'schema_version': 1,
                'python_requires': '>=3.11', 'artifacts': records}
    raw = json.dumps(manifest, sort_keys=True).encode()
    (directory / 'host-release.json').write_bytes(raw)
    return directory, hashlib.sha256(raw).hexdigest()



class HostDropinAlignmentTests(HermeticProcessFixture, unittest.TestCase):
    """Hand-made host drop-ins follow the entry point of the release that will run."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.root = self.home / '.local/share/agent-hub2'
        self.dropin = self.home / '.config/systemd/user' / (updater.UNITS[0] + '.d') / '90-strict-aplexer.conf'
        self.dropin.parent.mkdir(parents=True)
        self.legacy = ('[Service]\nExecStart=\nExecStart=%s/current/bin/hub2 serve --aplexer /x/a\n'
                       % self.root)
        self.dropin.write_text(self.legacy)
        self.calls = []
        # Default deny first; the recorder below stacks on top of it.
        self.install_process_guard()
        patcher = patch.multiple(updater, command=lambda args: self.calls.append([str(a) for a in args]))
        patcher.start()
        self.addCleanup(patcher.stop)
        home = patch.object(Path, 'home', return_value=self.home)
        home.start()
        self.addCleanup(home.stop)

    def current(self, *, legacy):
        release = self.root / 'releases' / SHA
        (release / 'bin').mkdir(parents=True, exist_ok=True)
        (release / 'bin/hub2').unlink(missing_ok=True)
        if legacy:
            (release / 'bin/hub2').write_text('')
        link = self.root / 'current'
        link.unlink(missing_ok=True)
        link.symlink_to(release)

    def declare_entry_metadata(self, release, *, declared):
        """Installed dist-info entry-point metadata of a pinned release."""
        dist_info = release / 'lib/python3.12/site-packages' / 'agent_hub_host_v2-2.0.0.dist-info'
        dist_info.mkdir(parents=True, exist_ok=True)
        (dist_info / 'entry_points.txt').write_text(
            '[console_scripts]\nhub2 = agent_hub_host.cli:main\n' if declared else
            '[console_scripts]\nhub = agent_hub_host.wheel_cli:main\n')

    def recognized_launcher_bytes(self, release):
        """The genuine pip console-script launcher form for this release venv."""
        return ('#!' + os.path.abspath(release / 'bin/python') + '\n'
                '# -*- coding: utf-8 -*-\n'
                'import re\n'
                'import sys\n'
                'from agent_hub_host.cli import main\n'
                "if __name__ == '__main__':\n"
                "    sys.argv[0] = re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])\n"
                '    sys.exit(main())\n').encode()

    def test_release_without_hub2_gets_hub_host_and_reverts_for_a_legacy_release(self):
        self.current(legacy=False)
        updater.restart_host(self.root)
        self.assertIn('current/bin/hub host serve --aplexer /x/a', self.dropin.read_text())
        self.assertNotIn('/bin/hub2', self.dropin.read_text())
        self.assertEqual(self.calls, [['systemctl', '--user', 'daemon-reload'],
                                      ['systemctl', '--user', 'restart', updater.UNITS[0]]])
        self.calls.clear()
        updater.restart_host(self.root)  # already aligned: no reload
        self.assertEqual(self.calls, [['systemctl', '--user', 'restart', updater.UNITS[0]]])
        self.current(legacy=True)  # rollback to a release that still ships bin/hub2
        updater.restart_host(self.root)
        self.assertEqual(self.dropin.read_text(), self.legacy)

    def test_transitional_release_keeps_hub2_dropins_byte_identical(self):
        # The transition release declares both console entries (unification
        # rollout, see test_transitional_wheel_entry_contract). Unpinned
        # drop-ins follow the running release's bin/hub2 launcher, never its
        # entry metadata: the fleet's hub2-style drop-ins survive the
        # transition byte-identical and the strict pinned-entry parser is
        # not involved.
        release = self.root / 'releases' / SHA
        self.current(legacy=True)
        (release / 'bin/hub2').write_bytes(self.recognized_launcher_bytes(release))
        dist_info = release / 'lib/python3.12/site-packages/agent_hub_host_v2-2.0.0.dist-info'
        dist_info.mkdir(parents=True)
        (dist_info / 'entry_points.txt').write_text(
            '[console_scripts]\nhub = agent_hub_host.wheel_cli:main\nhub2 = agent_hub_host.cli:main\n')
        updater.restart_host(self.root)
        self.assertEqual(self.dropin.read_text(), self.legacy)
        self.assertEqual(self.calls, [['systemctl', '--user', 'restart', updater.UNITS[0]]])

    def test_no_hub2_cli_remains_in_packaging_units_or_installers(self):
        import re
        retired = re.compile(r'bin/hub2\b|Scripts.hub2\.exe|^hub2 = |\bhub2 (serve|enroll|heartbeat|self-update|--config)\b', re.M)
        paths = [REPO / 'pyproject.toml', REPO / 'deploy/host-install.ps1',
                 REPO / 'scripts/switch-host-release.py', *sorted((REPO / 'deploy').glob('*.service'))]
        for path in paths:
            self.assertIsNone(retired.search(path.read_text()), str(path))
        # host-install.sh only mentions the old name to retire a stale link.
        self.assertNotIn('ln -s "$ROOT/current/bin/hub2"', (REPO / 'deploy/host-install.sh').read_text())
        # The host wheel is the one sanctioned exception: the transitional
        # legacy entry (see test_transitional_wheel_entry_contract) ships
        # beside the unified entry, so no other retired spelling may appear
        # in the packaging file either.
        self.assertIsNone(retired.search((REPO / 'hub2/host/pyproject.toml').read_text().replace(
            'hub2 = "agent_hub_host.cli:main"', 'hub2 TransitionalEntry')), 'hub2/host/pyproject.toml')

    def test_transitional_wheel_entry_contract(self):
        # CLI unification rollout (intake b1ba4d6b): the deployed
        # pre-unification updaters on every enrolled host prepare a release
        # environment and probe daemon readiness through the legacy daemon
        # entry. A post-unification wheel without it fails those updaters in
        # about one second (entry check in their prepare_environment) and
        # the fleet deadlocks: the feed's newer updater bytes only reach
        # updater.py after a successful release activation, which never
        # happens. Both entries are therefore mandatory while the rollout
        # is in flight; dropping the legacy entry again is a reviewed
        # retirement for when every host runs a post-unification updater.
        import re
        import tomllib
        packaging = tomllib.loads((REPO / 'hub2/host/pyproject.toml').read_text())
        self.assertEqual(packaging['project']['scripts'], {
            'hub': 'agent_hub_host.wheel_cli:main',
            'hub2': 'agent_hub_host.cli:main',
        })
        # The legacy target is exact: the candidate updater recognizes and
        # keeps only a launcher whose body imports the daemon CLI main.
        self.assertIsNotNone(re.fullmatch(r'agent_hub_host\.cli:main',
                                          packaging['project']['scripts']['hub2']))

    def test_unrelated_drop_in_commands_are_left_alone(self):
        other = self.dropin.with_name('10-other.conf')
        other.write_text('[Service]\nExecStart=/usr/bin/hub2 serve\nEnvironment=X=/agent-hub2/current/bin/hub2x\n')
        self.current(legacy=False)
        updater.restart_host(self.root)
        self.assertEqual(other.read_text(), '[Service]\nExecStart=/usr/bin/hub2 serve\nEnvironment=X=/agent-hub2/current/bin/hub2x\n')

    def test_only_the_execstart_program_token_is_rewritten(self):
        owned = f'{self.root}/current'
        text = ('[Service]\n'
                f'Environment=HUB2_PROBE={owned}/bin/hub2\n'
                f'ExecStartPre={owned}/bin/python -c "assert 1"\n'
                f'# comment mentioning {owned}/bin/hub host\n'
                f'ExecStart={owned}/bin/hub2 serve --aplexer /x/a --extra\n')
        self.dropin.write_text(text)
        self.current(legacy=False)
        updater.restart_host(self.root)
        # Only the ExecStart program token changed; argv tail and every other
        # line, including exact owned-form strings elsewhere, stay identical.
        self.assertEqual(self.dropin.read_text(), (
            '[Service]\n'
            f'Environment=HUB2_PROBE={owned}/bin/hub2\n'
            f'ExecStartPre={owned}/bin/python -c "assert 1"\n'
            f'# comment mentioning {owned}/bin/hub host\n'
            f'ExecStart={owned}/bin/hub host serve --aplexer /x/a --extra\n'))

    def test_foreign_root_execstart_is_never_rewritten(self):
        other = self.dropin.with_name('10-foreign.conf')
        other.write_text(f'[Service]\nExecStart=/opt/other/agent-hub2/current/bin/hub2 serve\n'
                         f'Environment=Y=/opt/other/agent-hub2/venv/bin/hub host\n')
        self.current(legacy=False)
        updater.restart_host(self.root)
        self.assertEqual(other.read_text(), '[Service]\nExecStart=/opt/other/agent-hub2/current/bin/hub2 serve\n'
                         'Environment=Y=/opt/other/agent-hub2/venv/bin/hub host\n')

    def test_multiple_owned_execstart_definitions_are_refused(self):
        self.dropin.write_text(f'[Service]\nExecStart={self.root}/current/bin/hub2 serve\n'
                               f'ExecStart={self.root}/venv/bin/hub host serve\n')
        original = self.dropin.read_bytes()
        self.current(legacy=False)
        with self.assertRaisesRegex(ValueError, 'ambiguous host ExecStart'):
            updater.restart_host(self.root)
        self.assertEqual(self.dropin.read_bytes(), original)
        self.assertEqual(self.calls, [])  # refused before reload or restart

    def test_multiline_owned_execstart_is_refused(self):
        self.dropin.write_text(f'[Service]\nExecStart={self.root}/current/bin/hub2 serve \\\n'
                               '  --aplexer /x/a\n')
        original = self.dropin.read_bytes()
        self.current(legacy=False)
        with self.assertRaisesRegex(ValueError, 'multiline host ExecStart'):
            updater.restart_host(self.root)
        self.assertEqual(self.dropin.read_bytes(), original)
        self.assertEqual(self.calls, [])

    def test_owned_release_pin_keeps_its_verified_legacy_entry(self):
        pinned = self.dropin.with_name('80-pinned.conf')
        pinned.write_text(f'[Service]\nExecStart={self.root}/releases/{OLD_SHA}/bin/hub2 serve\n')
        old_release = self.root / 'releases' / OLD_SHA
        (old_release / 'bin').mkdir(parents=True)
        (old_release / 'bin/python').symlink_to(sys.executable)
        (old_release / 'hub.pyz').write_bytes(b'artifact:hub.pyz')
        (old_release / 'bin/hub').symlink_to('../hub.pyz')
        self.declare_entry_metadata(old_release, declared=True)
        (old_release / 'bin/hub2').write_bytes(self.recognized_launcher_bytes(old_release))
        self.current(legacy=False)  # the unified current capability cannot rewrite the pin
        updater.restart_host(self.root)
        self.assertEqual(pinned.read_text(),
                         f'[Service]\nExecStart={self.root}/releases/{OLD_SHA}/bin/hub2 serve\n')

    def test_owned_release_pin_follows_its_own_verified_capability(self):
        pin = 'c' * 40
        release = self.root / 'releases' / pin
        (release / 'bin').mkdir(parents=True)
        (release / 'hub.pyz').write_bytes(b'artifact:hub.pyz')
        (release / 'bin/hub').symlink_to('../hub.pyz')  # the recognized unified operator launcher
        self.declare_entry_metadata(release, declared=False)  # its own metadata: no hub2 entry
        pinned = self.dropin.with_name('80-pinned.conf')
        pinned.write_text(f'[Service]\nExecStart={self.root}/releases/{pin}/bin/hub2 serve\n')
        self.current(legacy=True)  # the legacy current capability cannot fix the pin either
        updater.restart_host(self.root)
        self.assertEqual(pinned.read_text(),
                         f'[Service]\nExecStart={self.root}/releases/{pin}/bin/hub host serve\n')

    def test_pinned_unified_declaration_requires_positive_host_metadata(self):
        # staticv2 review R4: merely readable metadata never declares "no
        # hub2 entry". Empty, malformed, target-less, ambiguous or unrelated
        # dist-info content is an unknown contract, so a pinned release that
        # lost bin/hub2 but keeps the legacy `bin/hub -> ../hub.pyz` operator
        # launcher is refused unchanged instead of being misread as a
        # unified capability. Only the release's own positive `hub =
        # agent_hub_host.wheel_cli:main` host declaration proves the pin.
        pin = 'd' * 40
        release = self.root / 'releases' / pin
        (release / 'bin').mkdir(parents=True)
        (release / 'hub.pyz').write_bytes(b'artifact:hub.pyz')
        (release / 'bin/hub').symlink_to('../hub.pyz')  # legacy releases ship this launcher too
        dist_root = release / 'lib/python3.12/site-packages'
        pinned = self.dropin.with_name('80-pinned.conf')
        pinned.write_text(f'[Service]\nExecStart={self.root}/releases/{pin}/bin/hub2 serve\n')
        original = pinned.read_bytes()
        self.current(legacy=True)  # neither current capability may be borrowed onto the pin
        for case, dist_name, content in [
            ('empty host metadata', 'agent_hub_host_v2-2.0.0.dist-info', ''),
            ('no console_scripts section', 'agent_hub_host_v2-2.0.0.dist-info',
             '[metadata]\nname = agent_hub_host_v2\n'),
            ('malformed console_scripts', 'agent_hub_host_v2-2.0.0.dist-info',
             '[console_scripts]\nhub agent_hub_host.wheel_cli:main\n'),
            ('unsupported entry target', 'agent_hub_host_v2-2.0.0.dist-info',
             '[console_scripts]\nhub = agent_hub_host.cli:main\n'),
            ('ambiguous declarations', 'agent_hub_host_v2-2.0.0.dist-info',
             '[console_scripts]\nhub = agent_hub_host.wheel_cli:main\nhub2 = agent_hub_host.cli:main\n'),
            ('unrelated distribution', 'websockets-16.0.dist-info',
             '[console_scripts]\nhub = agent_hub_host.wheel_cli:main\n'),
        ]:
            if dist_root.exists():
                shutil.rmtree(dist_root)
            dist_info = dist_root / dist_name
            dist_info.mkdir(parents=True)
            (dist_info / 'entry_points.txt').write_text(content)
            with self.subTest(case=case):
                with self.assertRaisesRegex(ValueError, 'unverifiable pinned release entry contract'):
                    updater.restart_host(self.root)
                self.assertEqual(pinned.read_bytes(), original)
                self.assertEqual(self.calls, [])
        # The genuinely recognized unified declaration is exactly what
        # turns these refusals into a verified capability (the unrelated
        # distribution beside it proves nothing and stays ignored).
        dist_info = dist_root / 'agent_hub_host_v2-2.0.0.dist-info'
        dist_info.mkdir(parents=True)
        (dist_info / 'entry_points.txt').write_text(
            '[console_scripts]\nhub = agent_hub_host.wheel_cli:main\n')
        updater.restart_host(self.root)
        self.assertEqual(pinned.read_text(),
                         f'[Service]\nExecStart={self.root}/releases/{pin}/bin/hub host serve\n')

    def test_pinned_duplicate_entry_declarations_are_unknown_contracts(self):
        # staticv3 review R4: duplicated declarations are ambiguous metadata
        # and never resolve by last-wins. A foreign target followed by the
        # genuine unified target, the reverse order, a repeated identical
        # key, a repeated legacy key or a repeated console_scripts section
        # all leave the pinned contract unknown: the pin bytes stay exactly
        # unchanged with no service effect, and the single genuine unified
        # declaration remains the one contract that verifies the pin.
        pin = 'e' * 40
        release = self.root / 'releases' / pin
        (release / 'bin').mkdir(parents=True)
        (release / 'hub.pyz').write_bytes(b'artifact:hub.pyz')
        (release / 'bin/hub').symlink_to('../hub.pyz')
        dist_root = release / 'lib/python3.12/site-packages'
        pinned = self.dropin.with_name('80-pinned.conf')
        pinned.write_text(f'[Service]\nExecStart={self.root}/releases/{pin}/bin/hub2 serve\n')
        original = pinned.read_bytes()
        self.current(legacy=True)  # neither current capability may be borrowed onto the pin
        for case, content in [
            ('duplicate conflicting targets, foreign first',
             '[console_scripts]\nhub = foreign_module:main\nhub = agent_hub_host.wheel_cli:main\n'),
            ('duplicate conflicting targets, unified first',
             '[console_scripts]\nhub = agent_hub_host.wheel_cli:main\nhub = foreign_module:main\n'),
            ('duplicate identical unified key',
             '[console_scripts]\nhub = agent_hub_host.wheel_cli:main\nhub = agent_hub_host.wheel_cli:main\n'),
            ('duplicate legacy key',
             '[console_scripts]\nhub2 = agent_hub_host.cli:main\nhub2 = agent_hub_host.cli:main\n'),
            ('repeated console_scripts sections',
             '[console_scripts]\nhub = agent_hub_host.wheel_cli:main\n'
             '[metadata]\nname = agent_hub_host_v2\n'
             '[console_scripts]\nhub2 = agent_hub_host.cli:main\n'),
        ]:
            if dist_root.exists():
                shutil.rmtree(dist_root)
            dist_info = dist_root / 'agent_hub_host_v2-2.0.0.dist-info'
            dist_info.mkdir(parents=True)
            (dist_info / 'entry_points.txt').write_text(content)
            with self.subTest(case=case):
                with self.assertRaisesRegex(ValueError, 'unverifiable pinned release entry contract'):
                    updater.restart_host(self.root)
                self.assertEqual(pinned.read_bytes(), original)
                self.assertEqual(self.calls, [])
        # The unambiguous single genuine declaration is exactly what turns
        # the same pin into a verified unified capability.
        if dist_root.exists():
            shutil.rmtree(dist_root)
        dist_info = dist_root / 'agent_hub_host_v2-2.0.0.dist-info'
        dist_info.mkdir(parents=True)
        (dist_info / 'entry_points.txt').write_text(
            '[console_scripts]\nhub = agent_hub_host.wheel_cli:main\n')
        updater.restart_host(self.root)
        self.assertEqual(pinned.read_text(),
                         f'[Service]\nExecStart={self.root}/releases/{pin}/bin/hub host serve\n')

    def test_pinned_legacy_declaration_without_its_launcher_is_refused_unchanged(self):
        # The causal recovery case: a retained legacy release lost bin/hub2
        # and only its old `bin/hub -> hub.pyz` zipapp remains, without a
        # host command. bin/hub presence never proves a unified capability,
        # and the current release's capability is never borrowed: the pin
        # contract is unknown and the drop-in is refused unchanged.
        pin = OLD_SHA
        release = self.root / 'releases' / pin
        (release / 'bin').mkdir(parents=True)
        (release / 'hub.pyz').write_bytes(b'old-archive:hub.pyz')
        (release / 'bin/hub').symlink_to('../hub.pyz')
        self.declare_entry_metadata(release, declared=True)
        pinned = self.dropin.with_name('80-pinned.conf')
        pinned.write_text(f'[Service]\nExecStart={self.root}/releases/{pin}/bin/hub2 serve\n')
        original = pinned.read_bytes()
        self.current(legacy=False)
        with self.assertRaisesRegex(ValueError, 'unverifiable pinned release entry contract'):
            updater.restart_host(self.root)
        self.assertEqual(pinned.read_bytes(), original)
        self.assertEqual(self.calls, [])  # refused before reload or restart

    def test_pinned_unrecognized_hub2_file_is_never_a_verified_legacy_entry(self):
        # An empty placeholder bin/hub2 (the old broken repair's shape) is
        # not the managed launcher even when the release metadata declares
        # the entry; the pin is refused unchanged, never declared legacy.
        release = self.root / 'releases' / OLD_SHA
        (release / 'bin').mkdir(parents=True)
        (release / 'bin/python').symlink_to(sys.executable)
        self.declare_entry_metadata(release, declared=True)
        (release / 'bin/hub2').write_text('')
        pinned = self.dropin.with_name('80-pinned.conf')
        pinned.write_text(f'[Service]\nExecStart={self.root}/releases/{OLD_SHA}/bin/hub2 serve\n')
        original = pinned.read_bytes()
        self.current(legacy=False)
        with self.assertRaisesRegex(ValueError, 'unverifiable pinned release entry contract'):
            updater.restart_host(self.root)
        self.assertEqual(pinned.read_bytes(), original)
        self.assertEqual(self.calls, [])  # refused before reload or restart

    def test_pinned_entry_without_installed_metadata_is_refused_unchanged(self):
        # A launcher-shaped bin/hub2 without the release's own installed
        # entry-point metadata is an unknown contract, not a verified
        # legacy capability.
        release = self.root / 'releases' / OLD_SHA
        (release / 'bin').mkdir(parents=True)
        (release / 'bin/python').symlink_to(sys.executable)
        (release / 'bin/hub2').write_bytes(self.recognized_launcher_bytes(release))
        pinned = self.dropin.with_name('80-pinned.conf')
        pinned.write_text(f'[Service]\nExecStart={self.root}/releases/{OLD_SHA}/bin/hub2 serve\n')
        original = pinned.read_bytes()
        self.current(legacy=False)
        with self.assertRaisesRegex(ValueError, 'unverifiable pinned release entry contract'):
            updater.restart_host(self.root)
        self.assertEqual(pinned.read_bytes(), original)
        self.assertEqual(self.calls, [])  # refused before reload or restart

    def test_unverifiable_release_pin_is_refused_unchanged(self):
        pinned = self.dropin.with_name('80-pinned.conf')
        pinned.write_text(f'[Service]\nExecStart={self.root}/releases/{OLD_SHA}/bin/hub2 serve\n')
        original = pinned.read_bytes()
        self.current(legacy=False)  # no releases/OLD_SHA directory: contract unknown
        with self.assertRaisesRegex(ValueError, 'unverifiable pinned release entry contract'):
            updater.restart_host(self.root)
        self.assertEqual(pinned.read_bytes(), original)
        self.assertEqual(self.calls, [])  # refused before reload or restart

    def test_owned_path_in_a_foreign_program_argument_is_never_rewritten(self):
        other = self.dropin.with_name('10-foreign-arg.conf')
        text = (f'[Service]\n'
                f'ExecStart=/usr/bin/printf {self.root}/current/bin/hub2 %s\\n\n'
                f'ExecStartPost=/bin/cat {self.root}/venv/bin/hub host\n')
        other.write_text(text)
        self.current(legacy=False)
        updater.restart_host(self.root)
        self.assertEqual(other.read_text(), text)


class FixtureDiscoveryContractTests(unittest.TestCase):
    """Discovery contract for the two guarded classes (staticv2 review R1):
    a shared fixture must only ever be mixed into unittest.TestCase, so
    every named test stays collected with working addCleanup/assertions,
    and the collected counts stay at the exactly audited numbers."""

    EXPECTED_TEST_COUNTS = {'ReleaseTests': 102, 'HostDropinAlignmentTests': 18}

    def test_guarded_classes_remain_unittest_test_cases(self):
        for name in self.EXPECTED_TEST_COUNTS:
            cls = globals()[name]
            with self.subTest(cls=name):
                self.assertTrue(issubclass(cls, unittest.TestCase), name)
                self.assertIn(unittest.TestCase, cls.__mro__)

    def test_guarded_classes_collect_their_exact_named_tests(self):
        for name, expected in self.EXPECTED_TEST_COUNTS.items():
            with self.subTest(cls=name):
                discovered = unittest.defaultTestLoader.loadTestsFromTestCase(globals()[name])
                self.assertEqual(discovered.countTestCases(), expected)


class AlertClientPackagingTests(unittest.TestCase):
    def setUp(self):
        scratch = REPO / '.local/tmp'
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch, prefix='test-client-packaging-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.client = (REPO / 'scripts/hub_alert_client.py').read_bytes()

    def test_export_client_uses_committed_bytes_not_dirty_or_untracked_source(self):
        repo = self.directory / 'repo'
        repo.mkdir()
        names = ['hub2/host/pyproject.toml', 'host/agent_hub_host/__init__.py',
                 'config/model-routing.json', 'scripts/build-hub-cli.py',
                 'scripts/session-host-supervisor.py', 'scripts/hub2-self-update.py',
                 'scripts/hub2-enroll.py', 'scripts/build-quota-dispatcher.py',
                 'scripts/hub-quota-publish.py', 'scripts/hub_alert_client.py',
                 'scripts/natural-quota-prelude.py', 'deploy/host-install.sh',
                 *(f'scripts/{name}' for name in builder.NATURAL_HELPERS),
                 'deploy/' + builder.WINDOWS_HOST_INSTALLER, 'uv.lock',
                 *(f'deploy/{name}' for name in builder.UNITS),
                 *(f'public/cli/{name}' for name in builder.CLI_INSTALLERS)]
        for name in names:
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.client if name == 'scripts/hub_alert_client.py' else b'synthetic source\n')
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
        def git(*args):
            return subprocess.run(['git', '-C', str(repo), *args], env=env, check=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10).stdout
        git('init', '--initial-branch=main')
        git('add', '--all')
        git('-c', 'user.name=Synthetic release fixture', '-c', 'user.email=fixture@example.invalid',
            'commit', '-m', 'Synthetic committed release source\n\nScope: in')
        commit = git('rev-parse', 'HEAD').decode().strip()
        (repo / 'scripts/hub_alert_client.py').write_bytes(b'raise RuntimeError("uncommitted client must not ship")\n')
        (repo / 'scripts/hub_alert_client_private.py').write_bytes(b'untracked fixture data\n')
        target = self.directory / 'export'
        target.mkdir()
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(builder.export_source(repo, commit, target), commit)
        self.assertEqual((target / 'scripts/hub_alert_client.py').read_bytes(), self.client)
        self.assertFalse((target / 'scripts/hub_alert_client_private.py').exists())

    def test_build_exports_standalone_client_with_exact_manifest_hash_and_size(self):
        repo = self.directory / 'build-repo'
        repo.mkdir()
        output = self.directory / 'built'
        transport_bytes = b''
        def _make_transport_wheel():
            nonlocal transport_bytes
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, 'w') as wheel:
                for rel in ('websockets/__init__.py', 'websockets/exceptions.py', 'websockets/sync/client.py'):
                    wheel.writestr(rel, b'# synthetic websockets\n')
            transport_bytes = buf.getvalue()
            return hashlib.sha256(transport_bytes).hexdigest()
        transport_sha = _make_transport_wheel()
        def export(_repo, _revision, target):
            files = {
                'hub2/host/agent_hub_host/__init__.py': b'',
                'hub2/host/pyproject.toml': b'[project]\nname = "agent_hub_host_v2"\nversion = "2.0.0"\n',
                'uv.lock': (
                    '[[package]]\nname = "websockets"\nversion = "16.0"\n'
                    'wheels = [{url = "https://fixture.invalid/websockets-16.0-py3-none-any.whl", '
                    f'hash = "sha256:{transport_sha}", size = {len(transport_bytes)}}}]\n'
                ).encode(),
                'scripts/hub_alert_client.py': self.client,
                'scripts/build-quota-dispatcher.py': (REPO / 'scripts/build-quota-dispatcher.py').read_bytes(),
                'scripts/hub-quota-publish.py': (REPO / 'scripts/hub-quota-publish.py').read_bytes(),
                'scripts/natural-quota-prelude.py': (REPO / 'scripts/natural-quota-prelude.py').read_bytes(),
                **{f'scripts/{name}': (REPO / 'scripts' / name).read_bytes() for name in builder.NATURAL_HELPERS},
            }
            # Match the real exported release closure; these source bytes are not executed.
            for name in ('private_fs.py', 'win32_fs.py'):
                for package in ('host/agent_hub_host', 'hub2/host/agent_hub_host'):
                    files[f'{package}/{name}'] = (REPO / package / name).read_bytes()
            files['host/agent_hub_host/managed_claude_deliver.py'] = (
                REPO / 'host/agent_hub_host/managed_claude_deliver.py').read_bytes()
            files['host/agent_hub_host/windows_managed_claude_deliver.py'] = (
                REPO / 'host/agent_hub_host/windows_managed_claude_deliver.py').read_bytes()
            cli_source = """import hashlib,json,zipfile
from pathlib import Path
def git_sha(): raise AssertionError('builder must bind exported commit')
def build(out, version):
    with zipfile.ZipFile(out/'hub.pyz','w') as archive:
        archive.writestr('agent_hub_host/_build_info.py','BUILD_COMMIT = "'+git_sha()+'"\\n')
    digest=hashlib.sha256((out/'hub.pyz').read_bytes()).hexdigest()
    (out/'manifest.json').write_text(json.dumps({'built_from_commit':git_sha(),'sha256':digest}))
    (out/'hub.pyz.sha256').write_text(digest+'  hub.pyz\\n')
"""
            files['scripts/build-hub-cli.py'] = cli_source.encode()
            for name in ['scripts/hub2-self-update.py', 'scripts/hub2-enroll.py', 'deploy/host-install.sh',
                         'deploy/' + builder.WINDOWS_HOST_INSTALLER,
                         *(f'deploy/{name}' for name in builder.UNITS),
                         *(f'public/cli/{name}' for name in builder.CLI_INSTALLERS)]:
                files[name] = b'synthetic artifact, never executed\n'
            for name, data in files.items():
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            return SHA
        def build_command(args, **_kwargs):
            self.assertEqual(args[:3], [sys.executable, '-m', 'pip'])
            artifacts = Path(args[args.index('--wheel-dir')+1] if 'wheel' in args else args[args.index('--dest')+1])
            if 'wheel' in args:
                with zipfile.ZipFile(artifacts / HOST, 'w') as wheel:
                    for rel in builder.NATURAL_HOST_CLOSURE:
                        data = (f'HOST_VERSION = {SHA!r}\nPROTOCOL_VERSION = 1\n' if rel == '_release.py'
                                else '# synthetic host module\n')
                        wheel.writestr('agent_hub_host/' + rel, data)
            else:
                (artifacts / 'websockets-16.0-py3-none-any.whl').write_bytes(transport_bytes)
            return b''
        with patch.object(builder, 'export_source', side_effect=export), patch.object(builder, 'run', side_effect=build_command):
            manifest = builder.build(repo, SHA, output)
        self.assertEqual((output / 'hub_alert_client.py').read_bytes(), self.client)
        self.assertEqual(manifest['artifacts']['hub_alert_client.py'],
                         {'sha256': hashlib.sha256(self.client).hexdigest(), 'size': len(self.client)})
        # The Windows host installer ships in every new release (intake 41ef7048).
        installer = (output / builder.WINDOWS_HOST_INSTALLER).read_bytes()
        self.assertEqual(manifest['artifacts'][builder.WINDOWS_HOST_INSTALLER],
                         {'sha256': hashlib.sha256(installer).hexdigest(), 'size': len(installer)})
        # Both Root helper artifacts are bound by exact hash and the 20-file cap is not raised.
        for name in builder.NATURAL_HELPERS:
            data = (REPO / 'scripts' / name).read_bytes()
            self.assertEqual((output / name).read_bytes(), data)
            self.assertEqual(manifest['artifacts'][name], {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)})
        self.assertLessEqual(len(manifest['artifacts']), 20)
        digest = hashlib.sha256((output / 'host-release.json').read_bytes()).hexdigest()
        self.assertEqual(builder.verify_bundle(output, SHA, digest), manifest)
        # Substituting or dropping one helper in the published set never passes.
        stage = output / 'hub-natural-stage.py'
        original = stage.read_bytes()
        stage.write_bytes(b'substituted' + original[11:])
        with self.assertRaisesRegex(ValueError, 'checksum mismatch: hub-natural-stage.py'):
            builder.verify_bundle(output, SHA, digest)
        stage.write_bytes(original)
        self.assertEqual(builder.verify_bundle(output, SHA, digest), manifest)

    def helper_bundle(self, names):
        bundle, _digest = publication_bundle(self.directory / ('helpers-' + '-'.join(n[:8] for n in names) or 'none'), SHA)
        manifest = json.loads((bundle / 'host-release.json').read_bytes())
        for name in names:
            data = b'inert helper data ' + name.encode()
            (bundle / name).write_bytes(data)
            manifest['artifacts'][name] = {'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}
        raw = json.dumps(manifest, sort_keys=True).encode()
        (bundle / 'host-release.json').write_bytes(raw)
        return bundle, hashlib.sha256(raw).hexdigest()

    def test_legacy_bundle_without_helpers_verifies_but_partial_or_orphan_helpers_do_not(self):
        legacy, digest = publication_bundle(self.directory / 'legacy', SHA)
        self.assertNotIn('hub-natural-stage.py', builder.verify_bundle(legacy, SHA, digest)['artifacts'])
        for names in (builder.NATURAL_HELPERS[:1], builder.NATURAL_HELPERS[1:], builder.NATURAL_HELPERS):
            with self.subTest(names=names):
                bundle, digest = self.helper_bundle(names)
                # Helpers alone, or one without the other, are a partial natural set.
                with self.assertRaisesRegex(ValueError, 'partial natural helper artifact set'):
                    builder.verify_bundle(bundle, SHA, digest)

    def test_missing_and_tampered_client_never_pass_verified_bundle(self):
        bundle, digest = publication_bundle(self.directory / 'bundle', SHA)
        self.assertIn('hub_alert_client.py', builder.verify_bundle(bundle, SHA, digest)['artifacts'])
        client = bundle / 'hub_alert_client.py'
        original = client.read_bytes()
        client.write_bytes(b'X' * len(original))
        with self.assertRaisesRegex(ValueError, 'checksum mismatch: hub_alert_client.py'):
            builder.verify_bundle(bundle, SHA, digest)
        client.unlink()
        with self.assertRaisesRegex(ValueError, 'missing or unexpected files'):
            builder.verify_bundle(bundle, SHA, digest)
        # Removing both record and file cannot retain the original CI pin.
        manifest = json.loads((bundle / 'host-release.json').read_bytes())
        del manifest['artifacts']['hub_alert_client.py']
        (bundle / 'host-release.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, 'differs from the CI bundle'):
            builder.verify_bundle(bundle, SHA, digest)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        scratch = REPO / '.local/tmp'
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch, prefix='test-publication-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / 'published'
        self.current = self.root / 'current'
        self.receipt = self.directory / 'publication.json'

    def publish(self, action, commit=None, digest=None, bundle=None, *, receipt=None, token=None, error=None):
        args = ['--root', str(self.root), action]
        if commit is not None:
            args += ['--commit', commit, '--manifest-sha256', digest]
        if action == 'stage':
            args += ['--bundle', str(bundle)]
        else:
            args += ['--receipt', str(receipt or self.receipt)]
        if token is not None:
            args += ['--transaction-token', token]
        result = subprocess.run([sys.executable, str(REPO / 'scripts/publish-hub2-release.py'), *args],
                                capture_output=True, text=True, timeout=10)
        if error is None:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(error, result.stderr)

    def stage(self, commit, label):
        bundle, digest = publication_bundle(self.directory / label, commit, label)
        self.publish('stage', commit, digest, bundle)
        return bundle, digest

    def test_new_publisher_can_restore_real_legacy_bundle_without_standalone_client(self):
        legacy, legacy_digest = publication_bundle(self.directory / 'legacy', OLD_SHA, 'legacy')
        manifest = json.loads((legacy / 'host-release.json').read_bytes())
        del manifest['artifacts']['hub_alert_client.py']
        (legacy / 'hub_alert_client.py').unlink()
        (legacy / 'host-release.json').write_text(json.dumps(manifest, sort_keys=True))
        legacy_digest = hashlib.sha256((legacy / 'host-release.json').read_bytes()).hexdigest()
        # Separately pinned old-layout bundle; never reuse a new CI digest.
        builder.verify_bundle(legacy, OLD_SHA, legacy_digest)
        self.publish('stage', OLD_SHA, legacy_digest, legacy)
        old_receipt = self.directory / 'legacy-publication.json'
        self.publish('activate', OLD_SHA, legacy_digest, receipt=old_receipt)
        _, new_digest = self.stage(SHA, 'new-client')
        self.publish('activate', SHA, new_digest)
        self.assertEqual(json.loads(self.receipt.read_bytes())['previous_manifest_sha256'], legacy_digest)
        self.publish('rollback')
        self.assertEqual(os.readlink(self.current), f'releases/{OLD_SHA}')
        retained = self.root / 'releases' / OLD_SHA
        self.assertFalse((retained / 'hub_alert_client.py').exists())
        self.assertEqual(builder.verify_bundle(retained, OLD_SHA, legacy_digest), manifest)
        self.assertTrue((self.root / 'releases' / SHA / 'hub_alert_client.py').is_file())

    def test_stage_is_immutable_and_reuses_only_exact_bundle(self):
        bundle, digest = self.stage(SHA, 'original')
        release = self.root / 'releases' / SHA
        inode = release.stat().st_ino
        self.assertEqual(release.stat().st_mode & 0o777, 0o555)
        self.assertTrue(all(path.stat().st_mode & 0o777 == 0o444 for path in release.iterdir()))
        self.assertFalse(self.current.is_symlink())
        self.publish('stage', SHA, digest, bundle)
        self.assertEqual(release.stat().st_ino, inode)
        rebuilt, rebuilt_digest = publication_bundle(self.directory / 'rebuilt', SHA, 'rebuilt')
        self.publish('stage', SHA, rebuilt_digest, rebuilt, error='manifest differs')
        self.assertEqual(release.stat().st_ino, inode)
        self.assertEqual((release / 'host-release.json').read_bytes(), (bundle / 'host-release.json').read_bytes())

    def test_stage_refuses_non_regular_files_from_the_upload_directory(self):
        bundle, digest = publication_bundle(self.directory / 'untrusted', SHA)
        installer = bundle / 'install.sh'
        installer.unlink()
        os.mkfifo(installer)
        self.publish('stage', SHA, digest, bundle, error='not a bounded regular file')
        self.assertFalse((self.root / 'releases' / SHA).exists())
        installer.unlink()
        installer.symlink_to(bundle / 'install.ps1')
        self.publish('stage', SHA, digest, bundle, error='publication failed')
        self.assertFalse((self.root / 'releases' / SHA).exists())

    def test_activate_and_rollback_initial_absence_are_idempotent(self):
        _, digest = self.stage(SHA, 'first')
        self.publish('activate', SHA, digest)
        self.assertEqual(os.readlink(self.current), f'releases/{SHA}')
        original_receipt = self.receipt.read_bytes()
        self.assertIsNone(json.loads(original_receipt)['previous'])
        self.assertEqual(self.receipt.stat().st_mode & 0o777, 0o600)
        self.publish('activate', SHA, digest)
        self.assertEqual(self.receipt.read_bytes(), original_receipt)
        self.publish('rollback')
        self.assertFalse(os.path.lexists(self.current))
        self.assertEqual(json.loads(self.receipt.read_bytes())['phase'], 'rolled_back')
        self.publish('rollback')
        self.publish('activate', SHA, digest, error='receipt cannot activate')

    def test_delayed_root_activation_is_fenced_even_before_a_receipt_exists(self):
        _, digest = self.stage(SHA, 'first')
        old_token, new_token = '1' * 32, '2' * 32
        token_file = self.receipt.parent / 'transaction-token'
        token_file.write_text(old_token + '\n')
        self.publish('rollback', token=old_token)
        self.assertFalse(self.receipt.exists())
        self.publish('activate', SHA, digest, token=old_token, error='cancelled publication')
        self.assertFalse(self.current.is_symlink())

        (token_file.parent / 'cli-publication-cancelled').unlink()
        token_file.write_text(new_token + '\n')
        self.publish('activate', SHA, digest, token=old_token, error='another deploy transaction')
        self.assertFalse(self.current.is_symlink())
        self.publish('activate', SHA, digest, token=new_token)
        self.assertEqual(os.readlink(self.current), f'releases/{SHA}')
        self.publish('commit', token=old_token, error='another deploy transaction')
        self.current.unlink()
        self.publish('commit', token=new_token, error='matched CLI assets are not active')
        self.current.symlink_to(f'releases/{SHA}')
        self.publish('commit', token=new_token)
        self.publish('commit', token=new_token)
        self.publish('rollback', token=new_token, error='already committed')
        self.assertEqual(os.readlink(self.current), f'releases/{SHA}')

    def test_receipt_precedes_switch_and_crash_retries_preserve_previous(self):
        _, old_digest = self.stage(OLD_SHA, 'old')
        self.publish('activate', OLD_SHA, old_digest, receipt=self.directory / 'old.json')
        _, digest = self.stage(SHA, 'next')
        publisher = module('publish-hub2-release')
        switch_pointer = publisher.switch_pointer

        def interrupted(root, pointer):
            pending = json.loads(self.receipt.read_bytes())
            self.assertEqual(pending['previous'], f'releases/{OLD_SHA}')
            self.assertEqual(pending['previous_manifest_sha256'], old_digest)
            self.assertEqual(pending['phase'], 'prepared')
            self.assertEqual(os.readlink(self.current), pending['previous'])
            switch_pointer(root, pointer)
            raise OSError('interrupted after atomic switch')

        with patch.object(publisher, 'switch_pointer', interrupted), self.assertRaisesRegex(OSError, 'interrupted'):
            publisher.activate(self.root, SHA, digest, self.receipt)
        self.assertEqual(os.readlink(self.current), f'releases/{SHA}')
        self.publish('activate', SHA, digest)
        self.assertEqual(json.loads(self.receipt.read_bytes())['phase'], 'active')
        self.publish('rollback')
        self.assertEqual(os.readlink(self.current), f'releases/{OLD_SHA}')
        self.publish('rollback')

    def test_corrupt_candidate_never_activates_and_does_not_prevent_rollback(self):
        _, digest = self.stage(SHA, 'candidate')
        artifact = self.root / 'releases' / SHA / 'hub2-enroll.py'
        original = artifact.read_bytes()
        artifact.chmod(0o644)
        artifact.write_bytes(b'x' * len(original))
        self.publish('activate', SHA, digest, error='unsafe ownership or permissions')
        artifact.chmod(0o444)
        self.publish('activate', SHA, digest, error='checksum mismatch')
        self.assertFalse(self.receipt.exists())
        self.assertFalse(self.current.is_symlink())
        artifact.chmod(0o644)
        artifact.write_bytes(original)
        artifact.chmod(0o444)
        self.publish('activate', SHA, digest)
        artifact.chmod(0o644)
        artifact.write_bytes(b'x' * len(original))
        self.publish('rollback')
        self.assertFalse(os.path.lexists(self.current))

    def test_rollback_refuses_unrelated_and_unmanaged_current(self):
        _, digest = self.stage(SHA, 'candidate')
        _, other_digest = self.stage(OLD_SHA, 'other')
        self.publish('activate', SHA, digest)
        self.publish('activate', OLD_SHA, other_digest, receipt=self.directory / 'other.json')
        before = self.receipt.read_bytes()
        self.publish('rollback', error='current does not belong')
        self.assertEqual(os.readlink(self.current), f'releases/{OLD_SHA}')
        self.current.unlink()
        self.current.write_text('unmanaged')
        self.publish('rollback', error='not a managed release pointer')
        self.assertEqual(self.current.read_text(), 'unmanaged')
        self.assertEqual(self.receipt.read_bytes(), before)

    def test_rollback_refuses_previous_same_sha_bundle_substitution(self):
        _, old_digest = self.stage(OLD_SHA, 'old')
        self.publish('activate', OLD_SHA, old_digest, receipt=self.directory / 'old.json')
        _, digest = self.stage(SHA, 'candidate')
        self.publish('activate', SHA, digest)
        substituted, _ = publication_bundle(self.directory / 'substituted', OLD_SHA, 'substituted')
        old = self.root / 'releases' / OLD_SHA
        old.chmod(0o755)
        old.rename(self.directory / 'retained-original')
        substituted.rename(old)
        for artifact in old.iterdir():
            artifact.chmod(0o444)
        old.chmod(0o555)
        self.publish('rollback', error='manifest differs')
        self.assertEqual(os.readlink(self.current), f'releases/{SHA}')

    def test_vm_transaction_publishes_applied_release_and_rolls_back_before_checkout(self):
        vm = self.directory / 'vm'
        vm.mkdir()

        def git(*args):
            return subprocess.check_output(['git', '-C', str(vm), *args], text=True).strip()

        git('init', '--quiet')
        git('config', 'user.name', 'Release Fixture')
        git('config', 'user.email', 'release@example.invalid')
        git('config', 'core.hooksPath', '/dev/null')
        checker = module('schema-rollback-preflight')
        schema_contract = REPO / 'deploy/schema-compatibility.json'
        schemas = json.loads(schema_contract.read_text())['schemas']
        db_dir = vm / '.local/hub2-db'
        db_dir.mkdir(parents=True)
        for binding, path in checker.SOURCES.items():
            source = vm / path
            source.parent.mkdir(parents=True, exist_ok=True)
            version = schemas[binding]['bridge_write']
            source.write_text(f'initializeSchema(ctx, this.db, {version}, SCHEMA);\n')
            with sqlite3.connect(db_dir / f'{binding}-fixture.sqlite') as db:
                db.execute('CREATE TABLE hub2_schema(singleton INTEGER PRIMARY KEY, version INTEGER)')
                db.execute('INSERT INTO hub2_schema VALUES(1, ?)', (version,))
        installer = vm / 'deploy/vm/install-hub2-node.sh'
        installer.parent.mkdir(parents=True)
        installer.write_text('set -eu\n[ "$1" = --hub-only ]\n[ ! -L .local/cli-releases/current ]\n'
                             'printf rebuilt > hub2/public/cli/manifest.json\n')
        (vm / '.gitignore').write_text('/.local/\n/.dev.vars\n/hub2/public/cli/\n')
        git('add', 'deploy', '.gitignore', 'hub2')
        git('commit', '--quiet', '-m', 'old release without publisher')
        previous = git('rev-parse', 'HEAD')
        git('branch', '-M', 'main')
        installer.write_text('set -eu\n[ "$*" = "--release-mode active --hub-only --schema-migrations defer --keep-cli-assets" ]\n'
                             '[ "$(cat hub2/public/cli/manifest.json)" = legacy ]\n')
        (vm / 'scripts').mkdir()
        for script in ('publish-hub2-release.py', 'build-hub2-release.py', 'hub2-self-update.py',
                       'schema-rollback-preflight.py', 'apply-model-policy.py', 'prepare-model-policy-service-credential.py'):
            shutil.copyfile(REPO / 'scripts' / script, vm / 'scripts' / script)
        shutil.copyfile(schema_contract, vm / 'deploy' / schema_contract.name)
        for path in checker.SOURCES.values():
            shutil.copyfile(REPO / path, vm / path)
        (vm / 'config').mkdir()
        shutil.copyfile(REPO / 'config/model-policy.json', vm / 'config/model-policy.json')
        git('add', 'scripts', 'deploy', 'hub2', 'config')
        git('commit', '--quiet', '-m', 'candidate release')
        candidate = git('rev-parse', 'HEAD')
        git('remote', 'add', 'origin', str(vm))
        git('checkout', '--quiet', '--detach', previous)
        (vm / '.dev.vars').write_text('HUB2_RELEASE_MODE=active\n')
        legacy_manifest = vm / 'hub2/public/cli/manifest.json'
        legacy_manifest.parent.mkdir(parents=True)
        legacy_manifest.write_text('legacy')
        upload_parent = vm / '.local/tmp'
        upload_parent.mkdir(parents=True)
        _, digest = publication_bundle(upload_parent / f'ci-cli-release-{candidate}', candidate)

        state = self.directory / 'deploy-state'
        # Reuse the offline protocol peer; execute the real protected policy
        # helper while retaining this fixture's CLI publication/rollback checks.
        from types import SimpleNamespace
        spec = importlib.util.spec_from_file_location('release_policy_transaction_fixture', REPO / 'scripts/tests/test_schema_release_transaction.py')
        policy_fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy_fixture)
        health_log = self.directory / 'health.log'
        policy_request_pointers = []

        def observe_commit(repo, *args):
            current = vm / '.local/cli-releases/current'
            policy_request_pointers.append(os.readlink(current) if current.is_symlink() else None)
            return git(*args)

        policy = policy_fixture.PolicyAPI(SimpleNamespace(repo=vm, state=state, health_log=health_log, candidate=candidate,
                                                         git=observe_commit))
        self.addCleanup(policy.close)
        prior = json.loads(json.dumps(policy.current['candidates']))
        prior[0]['weight'] = 37
        policy.current = {'version': 5, 'candidates': prior}
        credentials = self.directory / 'deploy-credentials'
        credentials.mkdir(mode=0o700)
        credential = credentials / 'model-policy-admin.json'
        credential.write_text(json.dumps(policy_fixture.service_document(target_sha=candidate)))
        credential.chmod(0o600)
        fake_bin = self.directory / 'fake-bin'
        fake_bin.mkdir()
        commands = {'sudo': 'exec "$@"', 'systemctl': 'exit 0', 'npm': 'exit 0',
                    'install': 'if [ "$1:$2" = -d:-o ]; then shift 5; set -- -d "$@"; '
                               'elif [ "$1" = -o ]; then shift 4; fi; exec "$MOCK_INSTALL" "$@"',
                    'python3': 'if [ "$1:$2" = "-:$MOCK_STATE" ]; then cat > "$MOCK_RETIRE"; '
                               'exec "$MOCK_PYTHON" "$@" < "$MOCK_RETIRE"; fi; exec "$MOCK_PYTHON" "$@"',
                    'curl': 'printf healthy >> "$MOCK_HEALTH_LOG"; printf "%s\\n" "$MOCK_HEALTH"'}
        for name, body in commands.items():
            path = fake_bin / name
            path.write_text('#!/bin/sh\n' + body + '\n')
            path.chmod(0o755)
        env = {**os.environ, 'PATH': f'{fake_bin}:{os.environ["PATH"]}', 'AGENT_HUB_DEPLOY_TEST_MODE': '1',
               'AGENT_HUB_TEST_REPO': str(vm), 'AGENT_HUB_TEST_TRANSACTION_DIR': str(state),
               'AGENT_HUB_TEST_CADDY_CONFIG': str(self.directory / 'unused-Caddyfile'),
               'AGENT_HUB_TEST_POLICY_URL': policy.url, 'AGENT_HUB_TEST_POLICY_CREDENTIAL': str(credential),
               'MOCK_HEALTH_LOG': str(health_log), 'HUB2_TOKEN': 'ambient-token-must-not-be-used',
               'MOCK_INSTALL': shutil.which('install'),
               'MOCK_STATE': str(state), 'MOCK_RETIRE': str(self.directory / 'retire.py'), 'MOCK_PYTHON': sys.executable,
               'MOCK_HEALTH': '{"ok":true,"service":"hub2"}'}

        def transaction(*args, expected_error=None, overrides=None):
            result = subprocess.run(['bash', str(REPO / 'deploy/vm/deploy-release-transaction.sh'), *args],
                                    env={**env, **(overrides or {})}, capture_output=True, text=True, timeout=10)
            if expected_error is None:
                self.assertEqual(result.returncode, 0, result.stderr)
            else:
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected_error, result.stderr)

        pointer = vm / '.local/cli-releases/current'
        transaction('apply', candidate, 'active', 'hub-only', 'matched-cli')
        self.assertEqual(policy.current['version'], 6)
        self.assertEqual(json.loads((policy_fixture.policy_directory(state) / 'receipt.json').read_text())['phase'], 'applied')
        self.assertEqual(legacy_manifest.read_text(), 'legacy')
        self.assertEqual((state / 'schema-mode').read_text().strip(), 'defer')
        self.assertEqual((state / 'cli-legacy.previous/manifest.json').read_text(), 'legacy')
        with (upload_parent / '.deploy-transaction.lock').open('ab') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            transaction('rollback', expected_error='another Hub2 deploy transaction')
        transaction('publish', candidate, digest, expected_error='active Hub2 API',
                    overrides={'MOCK_HEALTH': '{"status":"bootstrap","dispatch_enabled":false}'})
        self.assertFalse((state / 'cli-assets.json').exists())
        transaction('publish', candidate, 'f' * 64, expected_error='manifest differs')
        transaction('commit', expected_error='matched CLI assets were not published')
        self.assertEqual(json.loads((policy_fixture.policy_directory(state) / 'receipt.json').read_text())['phase'], 'applied',
                         'a premature finalize must not seal policy before CLI publication')
        self.assertFalse(pointer.is_symlink())
        transaction('publish', candidate, digest)
        transaction('publish', candidate, digest)
        self.assertEqual(os.readlink(pointer), f'releases/{candidate}')
        self.assertEqual(json.loads((state / 'cli-assets.json').read_text())['phase'], 'active')
        old_token = (state / 'transaction-token').read_text().strip()
        transaction('rollback')
        self.assertFalse(pointer.is_symlink())
        self.assertFalse(state.exists())
        self.assertEqual(git('rev-parse', 'HEAD'), previous)
        self.assertEqual(legacy_manifest.read_text(), 'legacy')
        self.assertEqual(policy.current, {'version': 7, 'candidates': prior})
        self.assertEqual(policy.writes()[-1][0], 'PUT')
        self.assertEqual(policy.writes()[-1][2], candidate, 'policy restoration precedes checkout')
        restore_index = next(i for i, call in enumerate(policy.calls) if call[0] == 'PUT')
        self.assertEqual(policy_request_pointers[restore_index], f'releases/{candidate}',
                         'policy restoration precedes published CLI pointer rollback')
        transaction('apply', candidate, 'active', 'hub-only', 'matched-cli')
        new_token = (state / 'transaction-token').read_text().strip()
        stale = subprocess.run([sys.executable, env['MOCK_RETIRE'], str(state), old_token],
                               capture_output=True, text=True, timeout=10)
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn('different deploy transaction', stale.stderr)
        self.assertEqual((state / 'transaction-token').read_text().strip(), new_token)
        transaction('publish', candidate, digest)
        transaction('commit')
        self.assertFalse(state.exists())
        self.assertEqual(os.readlink(pointer), f'releases/{candidate}')
        self.assertEqual(policy.current['version'], 8)
        self.assertEqual([call[0] for call in policy.writes()], ['POST', 'PUT', 'POST'])


class ManagedProfileRepinTests(unittest.TestCase):
    """Intake fc36c32b: non-Claude managed profiles follow the installed release."""

    def stale(self, archive, managed, label):
        code = ('import sys; sys.path.insert(0, ' + repr(archive) + '); import os; '
                'os.environ["AGENT_HUB_MANAGED_DIR"] = ' + repr(managed) + '; '
                'from agent_hub_host.provisioning import execute; execute()')
        return ['/usr/bin/python3.12', '-c', code, label]

    def write(self, root, block_profiles, outside=''):
        managed = root / 'managed'
        managed.mkdir()
        (managed / 'reconcile.lock').write_text('')
        target = root / 'releases' / SHA
        (target / 'bin').mkdir(parents=True)
        (target / 'bin/python').write_text('')
        (target / 'hub.pyz').write_bytes(b'PK')
        block = ''.join('\n[profiles.%s]\nengine = %s\ncommand = %s\n' % (name, json.dumps(engine), json.dumps(cmd))
                        for name, engine, cmd in block_profiles)
        config = root / 'aplexer' / 'config.toml'
        config.parent.mkdir()
        config.write_text('version = 1\n' + outside + '\n' + updater.MANAGED_BEGIN + block + updater.MANAGED_END)
        return config, target, managed

    def test_stale_recovery_archive_profile_is_repinned_to_the_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = str(root / 'managed')
            stale = self.stale('/home/x/.local/lib/agent-hub-recovery-cli/' + OLD_SHA + '/hub.pyz', managed, 'hub-antigravity')
            claude = ['/opt/r/' + OLD_SHA + '/bin/python', '-I', '-S', '-c', 'claude-pin', 'hub-claude']
            go = ['/m/engine-launch/shared-go.sh', 'shared-go']
            source = self.stale('/legacy/host', managed, 'shared-codex')
            config, target, _ = self.write(root, [('hub-hub-antigravity', 'antigravity', stale),
                                                  ('hub-hub-claude', 'claude', claude),
                                                  ('hub-shared-go', 'opencode', go),
                                                  ('hub-shared-codex', 'codex', source)],
                                           outside='\n[profiles.mine]\ncommand = ["x"]\n')
            with patch.dict(os.environ, {'APLEXER_CONFIG': str(config)}):
                self.assertEqual(updater.repin_managed_profiles(target), ['hub-hub-antigravity', 'hub-shared-codex'])
                self.assertEqual(updater.repin_managed_profiles(target), [])
            profiles = updater.tomllib.loads(config.read_text())['profiles']
            command = profiles['hub-hub-antigravity']['command']
            self.assertEqual(command[:4], [str(target / 'bin/python'), '-I', '-S', '-c'])
            self.assertIn(repr(str(target / 'hub.pyz')), command[4])
            self.assertIn(repr(managed), command[4])
            self.assertNotIn('recovery-cli', command[4])
            self.assertEqual(command[5], 'hub-antigravity')
            self.assertEqual(profiles['hub-hub-antigravity']['engine'], 'antigravity')
            # The fenced Claude pin, launcher scripts and
            # operator profiles stay byte-identical.
            self.assertEqual(profiles['hub-hub-claude']['command'], claude)
            self.assertEqual(profiles['hub-shared-go']['command'], go)
            # The maintained codex profile follows the release in the exact form
            # provisioning.module_command() generates (no -I -S, archive twice).
            archive = repr(str(target / 'hub.pyz'))
            self.assertEqual(profiles['hub-shared-codex']['command'], [
                str(target / 'bin/python'), '-c',
                'import sys; sys.path.insert(0, ' + archive + '); sys.path.insert(0, ' + archive + '); import os; '
                'os.environ["AGENT_HUB_MANAGED_DIR"] = ' + repr(managed) + '; '
                'from agent_hub_host.provisioning import execute; execute()', 'shared-codex'])
            self.assertEqual(profiles['mine']['command'], ['x'])

    def test_stale_engine_launch_script_is_repinned_to_the_release(self):
        import shlex
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = root / 'managed'
            launcher = managed / 'engine-launch' / 'shared-go.sh'
            config, target, _ = self.write(root, [('hub-shared-go', 'opencode', [str(launcher), 'shared-go'])])
            launcher.parent.mkdir(parents=True)
            stale = self.stale(str(root / 'releases' / OLD_SHA / 'hub.pyz'), str(managed), 'shared-go')
            launcher.write_text('#!/bin/sh\nexec ' + shlex.quote(str(root / 'releases' / OLD_SHA / 'bin/python'))
                                + ' -c ' + shlex.quote(stale[-2]) + ' "$@"\n')
            launcher.chmod(0o700)
            with patch.dict(os.environ, {'APLEXER_CONFIG': str(config)}):
                self.assertEqual(updater.repin_managed_profiles(target), ['hub-shared-go'])
                self.assertEqual(updater.repin_managed_profiles(target), [])
            text = launcher.read_text()
            self.assertIn(str(target / 'bin/python'), text)
            self.assertIn(str(target / 'hub.pyz'), text)
            self.assertNotIn(OLD_SHA, text)
            self.assertEqual(launcher.stat().st_mode & 0o777, 0o700)

    def test_three_argument_execute_generator_keeps_conservative_generic_output(self):
        target = Path('/synthetic/release')
        managed = '/synthetic/managed'
        for label in ('test-account', 'shared-codex'):
            with self.subTest(label=label):
                expected = [str(target / 'bin/python'), '-I', '-S',
                            *self.stale(str(target / 'hub.pyz'), managed, label)[1:]]
                self.assertEqual(updater.release_execute_command(target, label, managed), expected)
                self.assertEqual(updater.release_execute_command(target, label, managed, engine=None), expected)
        self.assertEqual(len(updater.release_execute_command(target, 'shared-codex', managed, engine='codex')), 4)

    def test_archive_owned_non_codex_canonical_name_keeps_generic_isolated_output(self):
        for engine in ('shell', 'zai', 'claude'):
            with self.subTest(engine=engine), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                managed = str(root / 'managed')
                stale = self.stale('/legacy/recovery/hub.pyz', managed, 'shared-codex')
                config, target, _ = self.write(root, [('hub-shared-codex', engine, stale)],
                                             outside='\n[profiles.operator]\ncommand = ["opaque"]\n')
                before = config.read_bytes()
                expected = [str(target / 'bin/python'), '-I', '-S',
                            *self.stale(str(target / 'hub.pyz'), managed, 'shared-codex')[1:]]
                with patch.dict(os.environ, {'APLEXER_CONFIG': str(config)}):
                    self.assertEqual(updater.repin_managed_profiles(target), ['hub-shared-codex'])
                    self.assertEqual(updater.repin_managed_profiles(target), [])
                profile = updater.tomllib.loads(config.read_text())['profiles']['hub-shared-codex']
                self.assertEqual(profile, {'engine': engine, 'command': expected})
                self.assertEqual(expected[-2].count('sys.path.insert'), 1)
                self.assertEqual(config.read_bytes(), before.replace(json.dumps(stale).encode(), json.dumps(expected).encode()))

    def test_source_tree_repin_requires_canonical_codex_engine_and_preserves_operator_routes(self):
        for engine, name in [('shell', 'hub-shared-codex'), ('zai', 'hub-shared-codex'), ('codex', 'hub-custom-codex')]:
            with self.subTest(engine=engine, name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = self.stale('/operator/source', str(root / 'managed'), name[4:])
                config, target, _ = self.write(root, [(name, engine, source)])
                before = config.read_bytes()
                with patch.dict(os.environ, {'APLEXER_CONFIG': str(config)}):
                    self.assertEqual(updater.repin_managed_profiles(target), [])
                self.assertEqual(config.read_bytes(), before)

    def test_label_mismatch_or_unmanaged_profile_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = str(root / 'managed')
            wrong = self.stale('/r/hub.pyz', managed, 'hub-claude')
            config, target, _ = self.write(root, [('hub-hub-antigravity', 'antigravity', wrong)])
            before = config.read_bytes()
            with patch.dict(os.environ, {'APLEXER_CONFIG': str(config)}):
                self.assertEqual(updater.repin_managed_profiles(target), [])
            self.assertEqual(config.read_bytes(), before)

    def test_missing_config_never_fails_the_update(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(os.environ, {'APLEXER_CONFIG': str(Path(tmp) / 'absent.toml')}):
            self.assertEqual(updater.repin_managed_profiles(Path(tmp) / 'releases' / SHA), [])



class RetainedSchemaReleaseTransactionTests(unittest.TestCase):
    """Required release-tooling explicitly executes the retained transaction suite."""
    def test_current_schema_transaction_contracts(self):
        spec = importlib.util.spec_from_file_location(
            'retained_schema_transaction_contract', REPO / 'scripts/tests/test_schema_release_transaction.py')
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(fixture.SchemaReleaseTransactionTest)
        count = suite.countTestCases()
        self.assertGreater(count, 0, 'retained transaction suite must not be empty')
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual(result.testsRun, count, 'every retained transaction case must execute')
        self.assertEqual(result.skipped, [], 'retained transaction contracts must not be skipped')
        self.assertTrue(result.wasSuccessful(), '\n'.join(
            str(case) + '\n' + detail for case, detail in result.failures + result.errors))


class RetainedSchemaRollbackPreflightTests(unittest.TestCase):
    """Required release-tooling explicitly executes the retained read-only preflight suite."""
    def test_current_schema_rollback_preflight_contracts(self):
        spec = importlib.util.spec_from_file_location(
            'retained_schema_rollback_preflight_contract', REPO / 'scripts/tests/test_schema_rollback_preflight.py')
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(fixture.SchemaRollbackPreflightTest)
        count = suite.countTestCases()
        self.assertGreater(count, 0, 'retained rollback preflight suite must not be empty')
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual(result.testsRun, count, 'every retained rollback preflight case must execute')
        self.assertEqual(result.skipped, [], 'retained rollback preflight contracts must not be skipped')
        self.assertTrue(result.wasSuccessful(), '\n'.join(
            str(case) + '\n' + detail for case, detail in result.failures + result.errors))


class RetainedSchemaInstallConfigurationTests(unittest.TestCase):
    """Required release-tooling executes the retained SchemaInstallConfigurationTests suite."""
    def test_current_schema_install_configuration_contracts(self):
        spec = importlib.util.spec_from_file_location(
            'retained_test_schema_install_configuration_contract', REPO / 'scripts/tests/test_schema_install_configuration.py')
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(fixture.SchemaInstallConfigurationTests)
        count = suite.countTestCases()
        self.assertGreater(count, 0, 'retained SchemaInstallConfigurationTests suite must not be empty')
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual(result.testsRun, count, 'every retained SchemaInstallConfigurationTests case must execute')
        self.assertEqual(result.skipped, [], 'retained SchemaInstallConfigurationTests contracts must not be skipped')
        self.assertTrue(result.wasSuccessful(), '\n'.join(
            str(case) + '\n' + detail for case, detail in result.failures + result.errors))


class RetainedSchemaInstallerGitOwnerTests(unittest.TestCase):
    """Required release-tooling executes the retained InstallerGitOwnerTests suite."""
    def test_current_schema_installer_git_owner_contracts(self):
        spec = importlib.util.spec_from_file_location(
            'retained_test_schema_installer_git_owner_contract', REPO / 'scripts/tests/test_schema_installer_git_owner.py')
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(fixture.InstallerGitOwnerTests)
        count = suite.countTestCases()
        self.assertGreater(count, 0, 'retained InstallerGitOwnerTests suite must not be empty')
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual(result.testsRun, count, 'every retained InstallerGitOwnerTests case must execute')
        self.assertEqual(result.skipped, [], 'retained InstallerGitOwnerTests contracts must not be skipped')
        self.assertTrue(result.wasSuccessful(), '\n'.join(
            str(case) + '\n' + detail for case, detail in result.failures + result.errors))


class RetainedSchemaObserverSelectionTests(unittest.TestCase):
    """Required release-tooling executes the retained ObserverSelectionTests suite."""
    def test_current_schema_observer_selection_contracts(self):
        spec = importlib.util.spec_from_file_location(
            'retained_test_schema_installer_observer_selection_contract', REPO / 'scripts/tests/test_schema_installer_observer_selection.py')
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(fixture.ObserverSelectionTests)
        count = suite.countTestCases()
        self.assertGreater(count, 0, 'retained ObserverSelectionTests suite must not be empty')
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual(result.testsRun, count, 'every retained ObserverSelectionTests case must execute')
        self.assertEqual(result.skipped, [], 'retained ObserverSelectionTests contracts must not be skipped')
        self.assertTrue(result.wasSuccessful(), '\n'.join(
            str(case) + '\n' + detail for case, detail in result.failures + result.errors))


class RetainedSchemaDiagnosticStagingTests(unittest.TestCase):
    """Required release-tooling executes the retained DiagnosticStaging suite."""
    def test_current_schema_diagnostic_staging_contracts(self):
        spec = importlib.util.spec_from_file_location(
            'retained_test_schema_picker_diagnostic_staging_contract', REPO / 'scripts/tests/test_schema_picker_diagnostic_staging.py')
        fixture = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture)
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(fixture.DiagnosticStaging)
        count = suite.countTestCases()
        self.assertGreater(count, 0, 'retained DiagnosticStaging suite must not be empty')
        result = unittest.TestResult()
        suite.run(result)
        self.assertEqual(result.testsRun, count, 'every retained DiagnosticStaging case must execute')
        self.assertEqual(result.skipped, [], 'retained DiagnosticStaging contracts must not be skipped')
        self.assertTrue(result.wasSuccessful(), '\n'.join(
            str(case) + '\n' + detail for case, detail in result.failures + result.errors))

class WindowsReaderClosureTests(unittest.TestCase):
    def test_real_builder_pins_exact_reader_and_helper_source(self):
        builder = module('build-hub2-release')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package, host = root / 'package', root / 'host'
            package.mkdir(); host.mkdir()
            for name in builder.WINDOWS_READER_CLOSURE:
                raw = (REPO / 'host/agent_hub_host' / name).read_bytes()
                (host / name).write_bytes(raw)
                (package / name).write_bytes(raw)
            helper = b'synthetic reviewed helper source'
            (host / 'managed_claude_deliver.py').write_bytes(helper)
            consumer = b'synthetic Windows managed consumer source'
            (host / 'windows_managed_claude_deliver.py').write_bytes(consumer)
            builder.prepare_windows_reader(package, host)
            stamp = (package / '_credential_artifact_binding.py').read_text()
            self.assertIn(repr(hashlib.sha256(helper).hexdigest()), stamp)
            self.assertIn(repr(hashlib.sha256(consumer).hexdigest()), stamp)
            self.assertIn(repr('hub2-windows-managed-claude-consumer-v1'), stamp)
            self.assertIn(repr('hub2-windows-credential-artifacts-v1'), stamp)
            (package / 'win32_fs.py').write_bytes(b'foreign reader')
            before = (package / '_credential_artifact_binding.py').read_bytes()
            with self.assertRaises(ValueError):
                builder.prepare_windows_reader(package, host)
            self.assertEqual((package / '_credential_artifact_binding.py').read_bytes(), before)


class WheelHubEntryTests(unittest.TestCase):
    """The wheel-installed `hub` exposes the host namespace through the daemon seam."""

    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'agent-hub-host-wheel-cli', REPO / 'hub2/host/agent_hub_host/wheel_cli.py')
        self.wheel_cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.wheel_cli)

    def test_host_namespace_reexecutes_the_isolated_daemon_module(self):
        seen = {}
        def fake_execv(path, argv):
            seen['argv'] = argv
            raise SystemExit(0)
        with patch.object(self.wheel_cli.os, 'execv', fake_execv), \
             patch.object(self.wheel_cli.sys, 'executable', '/venv/bin/python'):
            with self.assertRaises(SystemExit):
                self.wheel_cli.main(['host', 'serve', '--once'])
        self.assertEqual(seen['argv'], ['/venv/bin/python', '-I', '-m', 'agent_hub_host.cli', 'serve', '--once'])

    def test_windows_launcher_returns_the_daemon_exit_code(self):
        recorded = []
        def fake_run(command):
            recorded.append(command)
            return subprocess.CompletedProcess(command, 7)
        with patch.object(self.wheel_cli.os, 'name', 'nt'), \
             patch.object(self.wheel_cli.subprocess, 'run', fake_run), \
             patch.object(self.wheel_cli.sys, 'executable', r'C:\venv\Scripts\python.exe'):
            self.assertEqual(self.wheel_cli.main(['host', 'version']), 7)
        self.assertEqual(recorded, [[r'C:\venv\Scripts\python.exe', '-I', '-m', 'agent_hub_host.cli', 'version']])

    def test_non_host_commands_refuse_with_operator_guidance(self):
        with patch.object(self.wheel_cli.sys, 'executable', '/venv/bin/python'):
            for argv in (['sessions', 'list'], ['enroll', '--root', '/tmp/x'], []):
                captured = io.StringIO()
                with patch.object(self.wheel_cli.sys, 'stderr', captured):
                    self.assertEqual(self.wheel_cli.main(argv), 2)
                self.assertIn('hub host', captured.getvalue())
            captured = io.StringIO()
            with patch.object(self.wheel_cli.sys, 'stdout', captured):
                self.assertEqual(self.wheel_cli.main(['--help']), 0)
            self.assertIn('wheel console entry', captured.getvalue())
            self.assertIn('hub host --help', captured.getvalue())


class HostInstallRetirementTests(unittest.TestCase):
    """Only hub2 links the maintained installer created are retired; others stay."""

    def setUp(self):
        scratch = REPO / '.local/tmp'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch, prefix='test-installer-links-')
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.root = self.home / '.local/share/agent-hub2'

    def retire_block(self):
        text = (REPO / 'deploy/host-install.sh').read_text()
        lines = text.splitlines()
        start = lines.index('if [ -L "$HOME/.local/bin/hub2" ]; then')
        end = next(i for i in range(start + 1, len(lines)) if lines[i] == 'fi')
        return '\n'.join(lines[start:end + 1])

    def run_block(self, target=None, *, clear=True):
        binary = self.home / '.local/bin'
        binary.mkdir(parents=True, exist_ok=True)
        link = binary / 'hub2'
        if clear:
            if link.is_symlink():
                link.unlink()
            elif link.exists():
                raise AssertionError('refusing to clear a non-link hub2 entry')
        if target is not None:
            link.symlink_to(target)
        env = dict(os.environ, HOME=str(self.home), ROOT=str(self.root))
        result = subprocess.run(['sh', '-c', self.retire_block()], env=env, capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return link

    def test_script_and_block_parse(self):
        subprocess.run(['sh', '-n', str(REPO / 'deploy/host-install.sh')], check=True, timeout=10)
        block = self.retire_block()
        self.assertNotIn('*/agent-hub2/*', block)  # the broad pattern is gone
        subprocess.run(['sh', '-n', '-c', block], check=True, timeout=10)

    def test_exact_owned_current_and_venv_targets_are_retired(self):
        for entry in ('current/bin/hub2', 'venv/bin/hub2'):
            link = self.run_block(str(self.root / entry))
            self.assertFalse(link.is_symlink() or link.exists(), entry)

    def test_foreign_and_unrelated_targets_are_preserved(self):
        for target in ('/opt/other/agent-hub2/current/bin/hub2', '/usr/bin/hub2',
                       str(self.root / 'releases/deadbeef/bin/hub2')):
            link = self.run_block(target)
            self.assertTrue(link.is_symlink(), target)
            self.assertEqual(os.readlink(link), target)

    def test_regular_hub2_file_is_untouched(self):
        binary = self.home / '.local/bin'
        binary.mkdir(parents=True)
        keep = binary / 'hub2'
        keep.write_text('#!/bin/sh\necho own tool\n')
        self.run_block(clear=False)
        self.assertTrue(keep.is_file())
        self.assertEqual(keep.read_text(), '#!/bin/sh\necho own tool\n')


class UpdaterHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'agent-hub2'

    def _manifest(self, updater_bytes):
        sha = hashlib.sha256(updater_bytes).hexdigest()
        wheel_name = f'agent_hub_host_v2-2.0.0+g{SHA}-py3-none-any.whl'
        return {
            'schema_version': 1,
            'host_version': SHA,
            'protocol_version': 1,
            'python_requires': '>=3.11',
            'artifacts': {
                wheel_name: {'sha256': '0' * 64, 'size': 1},
                'websockets-16.0-py3-none-any.whl': {'sha256': '0' * 64, 'size': 1},
                'hub.pyz': {'sha256': '0' * 64, 'size': 1},
                'hub2-enroll.py': {'sha256': '0' * 64, 'size': 1},
                'host-install.sh': {'sha256': '0' * 64, 'size': 1},
                'hub2-self-update.py': {'sha256': sha, 'size': len(updater_bytes)},
                'agent-hub2-host.service': {'sha256': '0' * 64, 'size': 1},
                'agent-hub2-host-update.service': {'sha256': '0' * 64, 'size': 1},
                'agent-hub2-host-update.timer': {'sha256': '0' * 64, 'size': 1},
            },
        }

    def test_main_hands_off_to_verified_candidate_updater_before_install(self):
        candidate_bytes = (
            b'import json, pathlib, sys\n'
            b'if "--help" in sys.argv[1:]:\n'
            b'    raise SystemExit(0)\n'
            b'out = pathlib.Path(sys.argv[sys.argv.index("--root") + 1]) / "handoff-seen.json"\n'
            b'out.write_text(json.dumps(sys.argv[1:]))\n'
        )
        manifest = self._manifest(candidate_bytes)
        payloads = {
            'https://example.test/cli/host-release.json': json.dumps(manifest).encode(),
            'https://example.test/cli/hub2-self-update.py': candidate_bytes,
        }
        with patch.object(updater, 'fetch', side_effect=lambda url, limit: payloads[url]), \
             patch.object(Path, 'home', return_value=Path(self.temporary.name)):
            rc = updater.main(['--base-url', 'https://example.test', '--root', str(self.root), '--no-restart'])
        self.assertEqual(rc, 0)
        seen = json.loads((self.root / 'handoff-seen.json').read_text())
        self.assertEqual(seen, ['--no-handoff', '--base-url', 'https://example.test',
                                '--root', str(self.root), '--no-restart'])
        self.assertFalse((self.root / 'updater.py').exists())

    def test_main_skips_handoff_when_updater_matches_or_no_handoff_passed(self):
        current_bytes = Path(updater.__file__).read_bytes()
        manifest = self._manifest(current_bytes)
        payloads = {
            'https://example.test/cli/host-release.json': json.dumps(manifest).encode(),
        }
        with patch.object(updater, 'fetch', side_effect=lambda url, limit: payloads[url]):
            self.assertIsNone(updater.maybe_handoff_updater('https://example.test', self.root, ['--no-restart']))

        seen = []
        def fake_install(*args, **kwargs):
            seen.append(updater.HANDOFF_ARGV.get())
        with patch.object(updater, 'install', fake_install), \
             patch.object(Path, 'home', return_value=Path(self.temporary.name)):
            self.assertEqual(updater.main(['--no-handoff', '--base-url', 'https://example.test',
                                           '--root', str(self.root)]), 0)
        self.assertEqual(seen, [None])

    def test_main_logs_exception_type_on_failure(self):
        manifest = self._manifest(b'print("candidate")\n')
        payloads = {
            'https://example.test/cli/host-release.json': json.dumps(manifest).encode(),
            'https://example.test/cli/hub2-self-update.py': b'tampered\n',
        }
        stderr = io.StringIO()
        with patch.object(updater, 'fetch', side_effect=lambda url, limit: payloads[url]), \
             patch.object(Path, 'home', return_value=Path(self.temporary.name)), \
             contextlib.redirect_stderr(stderr):
            rc = updater.main(['--base-url', 'https://example.test', '--root', str(self.root)])
        self.assertEqual(rc, 1)
        self.assertIn('Hub2 release update failed (ValueError)', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
