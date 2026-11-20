"""Focused tests for the default-inert Root natural staging helper.

Root, UID, trust-anchor and network facts are simulated: no real network, Root,
sudo, systemd or installation. Fixtures are real USER files with explicit modes.
"""
import ast
import hashlib
import importlib.util
import io
import itertools
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import types
import unittest
from unittest.mock import patch
import warnings
import zipfile

REPO = Path(__file__).resolve().parents[2]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, REPO / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


stage = load('scripts/hub-natural-stage.py', 'hub_natural_stage_under_test')
donor = load('scripts/hub-alert-natural-donor.py', 'hub_alert_natural_donor_for_schema')
primary = load('hub2/host/agent_hub_host/quota_natural_primary.py', 'quota_natural_primary_for_schema')

SHA = 'c' * 40
HOST_WHEEL = f'agent_hub_host_v2-2.0.0+g{SHA}-py3-none-any.whl'
PAYLOAD_BYTES = {name: f'# business {name}\n'.encode() for name in stage.PAYLOADS}


def zip_bytes(members, *, symlink=()):
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, data in members:
                info = zipfile.ZipInfo(name)
                if name in symlink:
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, data)
    return buffer.getvalue()


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Release:
    """A synthetic, internally consistent release; mutate then call manifest()."""

    def __init__(self, *, natural=True, archive_tweak=None, version=SHA):
        self.version = version
        self.host_wheel = f'agent_hub_host_v2-2.0.0+g{version}-py3-none-any.whl'
        self.marker = f'HOST_VERSION = {version!r}\nPROTOCOL_VERSION = 1\n'
        host = [(f'agent_hub_host/{n}', self.marker if n == '_release.py' else f'# host {n}\n')
                for n in stage.NATURAL_HOST_CLOSURE]
        transport = [('websockets/__init__.py', '# ws\n'), ('websockets/exceptions.py', '# ws e\n'),
                     ('websockets/sync/client.py', '# ws c\n')]
        self.wheel = zip_bytes(host)
        self.transport = zip_bytes(transport)
        sources = {name: sha(data.encode()) for name, data in host + transport}
        sources['__main__.py'] = sha(stage.NATURAL_MAIN_STUB.encode())
        for name, data in PAYLOAD_BYTES.items():
            sources['natural/' + name] = sha(data)
        inner = {'v': 1, 'entry': 'agent_hub_host.quota_dispatch_entry:main', 'sources': sources,
                 'host_release': {'commit': version, 'host_wheel_sha256': sha(self.wheel),
                                  'transport_wheel_sha256': sha(self.transport),
                                  'natural_payloads': {n: sha(d) for n, d in PAYLOAD_BYTES.items()}}}
        members = [('__main__.py', stage.NATURAL_MAIN_STUB), ('MANIFEST.json', json.dumps(inner))]
        members += [(n, d) for n, d in host + transport]
        members += [('natural/' + n, d) for n, d in PAYLOAD_BYTES.items()]
        if archive_tweak:
            members = archive_tweak(members)
        self.archive = zip_bytes(members)
        self.files = {'hub.pyz': b'cli', 'hub2-self-update.py': b'upd', 'hub2-enroll.py': b'enr',
                      'host-install.sh': b'ins', **{u: b'unit' for u in
                      ('agent-hub2-host.service', 'agent-hub2-host-update.service', 'agent-hub2-host-update.timer')},
                      self.host_wheel: self.wheel, stage.TRANSPORT_WHEEL: self.transport}
        if natural:
            self.files.update({stage.ARCHIVE: self.archive, **PAYLOAD_BYTES,
                               stage.HELPERS[0]: b'donor bytes', stage.HELPERS[1]: b'stage bytes'})

    def manifest(self, **overrides):
        value = {'schema_version': 1, 'host_version': self.version, 'protocol_version': 1,
                 'python_requires': '>=3.11',
                 'artifacts': {n: {'sha256': sha(d), 'size': len(d)} for n, d in self.files.items()}}
        value.update(overrides)
        return value

    def transport_for(self, manifest=None, **tweaks):
        blobs = dict(self.files)
        blobs.update(tweaks)
        blobs['host-release.json'] = json.dumps(manifest or self.manifest()).encode()
        return FakeTransport(blobs)


class FakeTransport:
    def __init__(self, blobs):
        self.blobs = blobs
        self.requested = []

    def get(self, name, limit):
        self.requested.append(name)
        if name not in self.blobs:
            raise stage.StageError('transport_failure')
        data = self.blobs[name]
        if len(data) > limit:
            raise stage.StageError('artifact_too_large')
        return data


class StageCase(unittest.TestCase):
    def setUp(self):
        old = os.umask(0o002)
        self.addCleanup(os.umask, old)
        # The selection format caps the gate path at 80 bytes, so the fixture root must be short.
        self.base = Path(tempfile.mkdtemp(prefix='ns-', dir='/tmp'))
        self.addCleanup(shutil.rmtree, self.base, True)
        self.uid = os.getuid()
        for sub in ('', 'lib', 'lib/natural', 'etc', 'var', 'run', 'run/gate', 'run/broker'):
            (self.base / sub).mkdir(exist_ok=True)
            (self.base / sub).chmod(0o755)
        for sub in ('run/gate', 'run/broker'):
            (self.base / sub).chmod(0o700)
        self.release_root = str(self.base / 'lib/natural/releases')
        self.profile = str(self.base / 'etc/natural-donor.json')
        self.ledger = str(self.base / 'var/ledger/ledger.json')
        for target, value in (('ROOT_UID', self.uid), ('TRUST_ANCHOR', str(self.base)),
                              ('RELEASE_ROOT', self.release_root), ('LEDGER_PATH', self.ledger)):
            patcher = patch.object(stage, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name, sub in (('gate_directory_for', 'run/gate'), ('broker_directory_for', 'run/broker')):
            patcher = patch.object(stage, name, lambda uid, sub=sub: str(self.base / sub))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cfg = {'v': 1, 'base_url': 'https://hub.example', 'target_user': 'tester',
                    'python_bin': '/usr/bin/python3.12', 'health_config_path': '/home/tester/.config/h.json'}

    def stage(self, release=None, transport=None):
        release = release or Release()
        return stage.stage_current(self.cfg, transport or release.transport_for(), uid=self.uid,
                                   release_root=self.release_root, profile_path=self.profile,
                                   ledger_path=self.ledger)

    def release_dir(self):
        return Path(self.release_root) / SHA

    def listing(self):
        root = Path(self.release_root)
        return sorted(p.name for p in root.iterdir()) if root.exists() else []


class SchemaTests(unittest.TestCase):
    """Pure outputs must satisfy the real read_selection / load_fixed_profile."""

    DIGESTS = {name: sha(data) for name, data in PAYLOAD_BYTES.items()}

    def test_selection_matches_actual_read_selection_and_roles(self):
        raw = stage.build_selection(SHA, 1000, '/home/u/.config/health.json', self.DIGESTS)
        with patch.object(primary, 'read_root_file', lambda path, digest, limit=0: raw):
            plan = primary.read_selection(f'{stage.RELEASE_ROOT}/{SHA}/selection.json', sha(raw))
        self.assertEqual(plan['quota_sha256'], self.DIGESTS['hub-quota-publish.py'])
        self.assertEqual(plan['client_sha256'], self.DIGESTS['hub_alert_client.py'])
        self.assertEqual(plan['prelude_sha256'], self.DIGESTS['natural-quota-prelude.py'])
        self.assertEqual(plan['roles']['quota_publish']['argv'], [])
        self.assertEqual(plan['roles']['quota_monitor']['argv'][3], '/home/u/.config/health.json')

    def test_profile_matches_actual_donor_required_fields(self):
        cfg = {'target_user': 'tester', 'python_bin': '/usr/bin/python3.11'}
        profile, raw = stage.build_profile(cfg, SHA, 1000, sha(b'entry'))
        with patch.object(donor, '_read_root_json', lambda *a, **k: json.loads(raw)):
            loaded = donor.load_fixed_profile()
        self.assertEqual(loaded, profile)
        self.assertEqual(loaded['entry_archive'], f'{donor.RELEASE_ROOT}/{SHA}/host.pyz')

    def test_malicious_selection_inputs_rejected(self):
        with self.assertRaises(stage.StageError):
            stage.validate_user_path('relative/path')
        with self.assertRaises(stage.StageError):
            stage.validate_user_path('/a/../b')


class ManifestTests(StageCase):
    def test_legacy_manifest_without_natural_artifacts_stages_nothing(self):
        result = self.stage(Release(natural=False))
        self.assertEqual(result['result'], 'legacy_no_natural_artifacts')
        self.assertEqual(self.listing(), [])
        self.assertFalse(Path(self.profile).exists())

    def test_partial_or_mixed_natural_sets_refused(self):
        full = Release()
        for missing in (stage.HELPERS[0], stage.HELPERS[1], 'hub_alert_client.py', stage.ARCHIVE,
                        'natural-quota-prelude.py', 'hub-quota-publish.py'):
            with self.subTest(missing=missing):
                release = Release()
                del release.files[missing]
                with self.assertRaises(stage.StageError) as ctx:
                    self.stage(release)
                self.assertEqual(str(ctx.exception), 'partial_natural_set')
        self.assertEqual(self.listing(), [])
        self.assertIn(stage.ARCHIVE, full.files)

    def test_manifest_schema_bounds(self):
        release = Release()
        good = release.manifest()
        cases = {
            'schema': dict(good, schema_version=2),
            'version': dict(good, host_version='X' * 40),
            'python': dict(good, python_requires='>=3.13'),
            'extra_field': dict(good, artifacts={**good['artifacts'], 'x': {'sha256': 'a' * 64, 'size': 1, 'e': 1}}),
            'oversize_artifact': dict(good, artifacts={**good['artifacts'], 'big.bin': {'sha256': 'a' * 64, 'size': stage.MAX_ARTIFACT + 1}}),
            'unsafe_name': dict(good, artifacts={**good['artifacts'], '../evil': {'sha256': 'a' * 64, 'size': 1}}),
            'too_many': dict(good, artifacts={**good['artifacts'], **{f'f{i}': {'sha256': 'a' * 64, 'size': 1} for i in range(8)}}),
        }
        for label, manifest in cases.items():
            with self.subTest(label), self.assertRaises(stage.StageError):
                stage.validate_manifest(manifest, '/usr/bin/python3.12')
        self.assertEqual(stage.validate_manifest(good, '/usr/bin/python3.12'), (HOST_WHEEL, True))

    def test_artifact_digest_and_size_mismatch_refused(self):
        release = Release()
        for name in (stage.ARCHIVE, 'hub-quota-publish.py'):
            with self.subTest(name):
                tampered = release.files[name][:-1] + b'X'
                with self.assertRaisesRegex(stage.StageError, 'artifact_digest_mismatch'):
                    self.stage(release, release.transport_for(**{name: tampered}))
        self.assertEqual(self.listing(), [])

    def test_oversize_payload_and_archive_refused_before_publication(self):
        release = Release()
        big = b'x' * (stage.MAX_PAYLOAD + 1)
        release.files['hub-quota-publish.py'] = big
        with self.assertRaisesRegex(stage.StageError, 'artifact_too_large'):
            self.stage(release)
        self.assertEqual(self.listing(), [])

    def test_oversize_manifest_refused(self):
        transport = FakeTransport({'host-release.json': b'{' + b' ' * stage.MAX_MANIFEST + b'}'})
        with self.assertRaises(stage.StageError):
            self.stage(Release(), transport)

    def test_only_exact_manifest_names_are_requested(self):
        release = Release()
        transport = release.transport_for()
        self.stage(release, transport)
        self.assertEqual(set(transport.requested),
                         {'host-release.json', stage.ARCHIVE, *stage.PAYLOADS, HOST_WHEEL, stage.TRANSPORT_WHEEL})


class ArchiveTests(StageCase):
    def run_tweak(self, tweak):
        release = Release(archive_tweak=tweak)
        with self.assertRaises(stage.StageError) as ctx:
            self.stage(release)
        self.assertEqual(self.listing(), [])
        self.assertFalse(Path(self.profile).exists())
        return str(ctx.exception)

    def test_traversal_absolute_backslash_and_symlink_members_refused(self):
        for bad in ('../escape.py', '/abs.py', 'a\\b.py', 'x/../y.py'):
            with self.subTest(bad):
                self.assertEqual(self.run_tweak(lambda m, bad=bad: m + [(bad, 'x')]), 'unsafe_archive_member')

    def test_symlink_member_refused(self):
        buffer = zip_bytes([('link.py', 'target')], symlink=('link.py',))
        with self.assertRaisesRegex(stage.StageError, 'unsafe_archive_member'):
            stage.read_zip_members(buffer)

    def test_duplicate_members_refused(self):
        self.assertEqual(self.run_tweak(lambda m: m + [m[0]]), 'archive_member_ambiguity')

    def test_extra_substituted_and_unbound_members_refused(self):
        self.assertEqual(self.run_tweak(lambda m: m + [('agent_hub_host/extra.py', 'x')]),
                         'archive_manifest_member_mismatch')

        def mutate(members):
            return [(n, 'evil\n' if n == 'agent_hub_host/agent.py' else d) for n, d in members]
        self.assertEqual(self.run_tweak(mutate), 'archive_member_checksum_mismatch')

        def consistent_substitute(members):
            inner = json.loads(dict(members)['MANIFEST.json'])
            out = []
            for n, d in members:
                if n == 'agent_hub_host/agent.py':
                    d = 'evil\n'
                    inner['sources'][n] = sha(d.encode())
                out.append((n, d))
            return [(n, json.dumps(inner) if n == 'MANIFEST.json' else d) for n, d in out]
        self.assertEqual(self.run_tweak(consistent_substitute), 'archive_module_differs_from_outer_wheel')

    def test_entry_stub_and_release_marker_mismatch_refused(self):
        def rewrite(target, text):
            def tweak(members):
                inner = json.loads(dict(members)['MANIFEST.json'])
                inner['sources'][target] = sha(text.encode())
                return [(n, json.dumps(inner) if n == 'MANIFEST.json' else text if n == target else d)
                        for n, d in members]
            return tweak
        self.assertEqual(self.run_tweak(rewrite('__main__.py', 'import os\n')), 'archive_entry_stub_mismatch')
        # The same-bytes wheel marker check fires first for a marker differing from the manifest.
        self.assertEqual(self.run_tweak(rewrite('agent_hub_host/_release.py', 'HOST_VERSION = "x"\n')),
                         'archive_module_differs_from_outer_wheel')

    def test_binding_to_different_commit_or_wheel_refused(self):
        def rebind(members):
            inner = json.loads(dict(members)['MANIFEST.json'])
            inner['host_release']['commit'] = 'e' * 40
            return [(n, json.dumps(inner) if n == 'MANIFEST.json' else d) for n, d in members]
        self.assertEqual(self.run_tweak(rebind), 'archive_binding_mismatch')

    def test_oversize_member_and_bomb_refused(self):
        big = 'x' * (stage.MAX_MEMBER + 1)
        self.assertEqual(self.run_tweak(lambda m: m + [('natural/huge.bin', big)]), 'archive_member_size')
        with self.assertRaisesRegex(stage.StageError, 'archive_member_ambiguity'):
            stage.read_zip_members(zip_bytes([(f'f{i}', 'x') for i in range(stage.MAX_MEMBERS + 1)]))

    def test_not_a_zip_refused(self):
        with self.assertRaisesRegex(stage.StageError, 'invalid_zip'):
            stage.read_zip_members(b'not a zip')

    def test_payload_provenance_mismatch_with_outer_manifest_refused(self):
        release = Release()
        release.files['hub_alert_client.py'] = b'# different client\n'
        with self.assertRaisesRegex(stage.StageError, 'archive_payload_mismatch|archive_binding'):
            self.stage(release)
        self.assertEqual(self.listing(), [])


class PublishTests(StageCase):
    def test_immutable_files_modes_selection_and_atomic_publish(self):
        result = self.stage()
        self.assertEqual(result['result'], 'staged')
        directory = self.release_dir()
        self.assertEqual(sorted(p.name for p in directory.iterdir()), sorted(stage.STAGE_FILES))
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
        for path in directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o444, path.name)
        release = Release()
        self.assertEqual((directory / 'host.pyz').read_bytes(), release.archive)
        for name, data in PAYLOAD_BYTES.items():
            self.assertEqual((directory / name).read_bytes(), data)
        selection = json.loads((directory / 'selection.json').read_text())
        self.assertEqual(selection['quota_path'], f'{self.release_root}/{SHA}/hub-quota-publish.py')
        self.assertEqual(self.listing(), [SHA])  # no scratch leaks
        # Helpers are verified for the set but never staged as installable program files.
        self.assertFalse((directory / stage.HELPERS[1]).exists())

    def test_same_release_is_idempotent(self):
        self.stage()
        before = {p.name: p.stat().st_ino for p in self.release_dir().iterdir()}
        self.assertEqual(self.stage()['result'], 'already_staged')
        self.assertEqual({p.name: p.stat().st_ino for p in self.release_dir().iterdir()}, before)

    def test_existing_release_must_byte_match_and_is_never_overwritten(self):
        self.stage()
        target = self.release_dir() / 'host.pyz'
        target.chmod(0o644)
        target.write_bytes(b'operator replaced')
        target.chmod(0o444)
        with self.assertRaisesRegex(stage.StageError, 'existing_release_mismatch'):
            self.stage()
        self.assertEqual(target.read_bytes(), b'operator replaced')

    def test_existing_release_with_extra_file_or_wrong_mode_refused(self):
        self.stage()
        extra = self.release_dir() / 'extra'
        extra.write_text('x')
        with self.assertRaisesRegex(stage.StageError, 'existing_release_mismatch'):
            self.stage()
        self.assertTrue(extra.exists())
        extra.unlink()
        (self.release_dir() / 'selection.json').chmod(0o644)
        with self.assertRaisesRegex(stage.StageError, 'existing_release_mismatch|untrusted_root_file'):
            self.stage()

    def test_foreign_nonempty_existing_release_dir_is_preserved(self):
        foreign = self.release_dir()
        foreign.mkdir(parents=True)
        foreign.chmod(0o755)
        (foreign / 'keep').write_text('data')
        with self.assertRaises(stage.StageError):
            self.stage()
        self.assertEqual((foreign / 'keep').read_text(), 'data')

    def test_failed_publish_removes_only_own_scratch_and_preserves_other_nodes(self):
        other = Path(self.release_root) / ('.stage-other-1-1')
        other.mkdir(parents=True)
        Path(self.release_root).chmod(0o755)
        other.chmod(0o700)
        (other / 'precious').write_text('x')
        older = Path(self.release_root) / ('d' * 40)
        older.mkdir()
        (older / 'old').write_text('old')
        real_rename = os.rename

        def failing(*args, **kwargs):
            raise OSError('simulated rename failure')
        with patch.object(stage.os, 'rename', failing), self.assertRaises(OSError):
            self.stage()
        self.assertEqual(sorted(self.listing()), sorted(['.stage-other-1-1', 'd' * 40]))
        self.assertEqual((other / 'precious').read_text(), 'x')
        self.assertEqual((older / 'old').read_text(), 'old')
        self.assertIs(os.rename, real_rename)

    def test_failed_write_cleans_scratch(self):
        calls = {'n': 0}
        real = stage._write_new

        def flaky(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 3:
                raise OSError('disk full')
            return real(*args, **kwargs)
        with patch.object(stage, '_write_new', flaky), self.assertRaises(OSError):
            self.stage()
        self.assertEqual(self.listing(), [])

    def test_untrusted_release_root_refused(self):
        Path(self.release_root).mkdir()
        Path(self.release_root).chmod(0o777)
        with self.assertRaisesRegex(stage.StageError, 'untrusted'):
            self.stage()
        self.assertEqual(os.listdir(self.release_root), [])

    def test_symlinked_release_root_refused(self):
        real = self.base / 'elsewhere'
        real.mkdir()
        real.chmod(0o755)
        os.symlink(real, self.release_root)
        with self.assertRaises(stage.StageError):
            self.stage()
        self.assertEqual(os.listdir(real), [])


class ProfileTests(StageCase):
    def profile_data(self):
        return json.loads(Path(self.profile).read_text())

    def test_initial_profile_written_only_when_no_custody_and_dirs_provisioned(self):
        result = self.stage()
        self.assertEqual(result['profile'], 'initial_written')
        self.assertFalse(result['gating'])
        self.assertEqual(stat.S_IMODE(Path(self.profile).stat().st_mode), 0o600)
        data = self.profile_data()
        self.assertEqual(data['release_commit'], SHA)
        self.assertEqual(data['entry_sha256'], sha(Release().archive))
        self.assertEqual(data['ledger_path'], self.ledger)
        self.assertTrue(Path(self.ledger).parent.is_dir())
        self.assertFalse(Path(self.ledger).exists())  # the donor, not this helper, writes the ledger
        self.assertEqual([n for n in os.listdir(self.base / 'etc')], ['natural-donor.json'])

    def test_unprovisioned_user_directories_leave_profile_unwritten(self):
        (self.base / 'run/gate').rmdir()
        result = self.stage()
        self.assertEqual(result['profile'], 'not_written')
        self.assertFalse(Path(self.profile).exists())
        self.assertTrue(self.release_dir().is_dir())  # immutable DATA is still staged

    def test_untrusted_user_directory_mode_leaves_profile_unwritten(self):
        (self.base / 'run/broker').chmod(0o755)
        self.assertEqual(self.stage()['profile'], 'not_written')

    def test_same_release_idempotent_leaves_profile_untouched_even_with_ledger(self):
        self.stage()
        original = Path(self.profile).read_bytes()
        mtime = Path(self.profile).stat().st_mtime_ns
        Path(self.ledger).write_text(json.dumps({'state': 'adopted'}))
        result = self.stage()
        self.assertEqual(result['profile'], 'unchanged_same_release')
        self.assertEqual(Path(self.profile).read_bytes(), original)
        self.assertEqual(Path(self.profile).stat().st_mtime_ns, mtime)

    def test_new_release_never_changes_existing_profile_and_reports_unknown(self):
        self.stage()
        original = Path(self.profile).read_bytes()
        newer = Release(version='d' * 40)
        result = stage.stage_current(self.cfg, newer.transport_for(), uid=self.uid,
                                     release_root=self.release_root, profile_path=self.profile,
                                     ledger_path=self.ledger)
        self.assertEqual(result['result'], 'staged')
        self.assertEqual(result['profile'], 'unchanged_custody_unknown')
        self.assertEqual(result['custody'], 'UNKNOWN')
        self.assertFalse(result['gating'])
        self.assertEqual(Path(self.profile).read_bytes(), original)
        self.assertTrue((Path(self.release_root) / SHA).is_dir())
        self.assertTrue((Path(self.release_root) / ('d' * 40)).is_dir())

    def test_existing_ledger_blocks_initial_profile_for_every_state(self):
        for state in ('unknown', 'adopted', 'rights_may_have_transferred', 'anything'):
            with self.subTest(state):
                ledger = Path(self.ledger)
                ledger.parent.mkdir(exist_ok=True)
                ledger.parent.chmod(0o700)
                ledger.write_text(json.dumps({'state': state}))
                result = self.stage()
                self.assertEqual(result['custody'], 'UNKNOWN')
                self.assertFalse(Path(self.profile).exists())
                self.assertEqual(json.loads(ledger.read_text()), {'state': state})
                ledger.unlink()

    def test_existing_gate_node_preserved_and_blocks_profile(self):
        node = self.base / 'run/gate/source-gate.sock'
        node.write_text('pre-existing node')
        result = self.stage()
        self.assertEqual(result['custody'], 'UNKNOWN')
        self.assertEqual(result['reason'], 'existing_gate_node_preserved')
        self.assertEqual(node.read_text(), 'pre-existing node')
        self.assertFalse(Path(self.profile).exists())

    def test_unknown_existing_profile_is_preserved_byte_for_byte(self):
        Path(self.profile).write_text('{"v": 1, "legacy": true}\n')
        Path(self.profile).chmod(0o600)
        before = Path(self.profile).read_bytes()
        result = self.stage()
        self.assertEqual(result['custody'], 'UNKNOWN')
        self.assertEqual(Path(self.profile).read_bytes(), before)

    def test_profile_appearing_during_publication_is_not_replaced(self):
        real = os.link

        def racing(src, dst, **kwargs):
            Path(self.profile).write_text('winner')
            return real(src, dst, **kwargs)
        with patch.object(stage.os, 'link', racing):
            result = self.stage()
        self.assertEqual(result['profile'], 'unchanged_custody_unknown')
        self.assertEqual(Path(self.profile).read_text(), 'winner')
        self.assertEqual(os.listdir(self.base / 'etc'), ['natural-donor.json'])  # temp removed


class ConfigTests(StageCase):
    def write(self, data, mode=0o600):
        path = self.base / 'etc/natural-stage.json'
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        path.chmod(mode)
        return str(path)

    def test_valid_config_loads_and_fixed_fields_are_exact(self):
        self.assertEqual(stage.load_config(self.write(self.cfg)), self.cfg)
        for key in list(self.cfg):
            with self.subTest(missing=key):
                bad = {k: v for k, v in self.cfg.items() if k != key}
                with self.assertRaises(stage.StageError):
                    stage.load_config(self.write(bad))
        with self.assertRaises(stage.StageError):
            stage.load_config(self.write({**self.cfg, 'extra': 1}))

    def test_bad_values_refused(self):
        bad_values = {
            'base_url': ['http://hub.example', 'https://u:p@hub.example', 'https://hub.example/path',
                         'https://127.0.0.1', 'https://hub.example:0', 'ftp://x', 'https://hub.example?x=1'],
            'python_bin': ['/tmp/python3.12', '/usr/bin/python3', '/usr/bin/python3.13'],
            'target_user': ['root;x', '', 'A'],
            'health_config_path': ['rel', '/a/../b', ''],
        }
        for key, values in bad_values.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(stage.StageError):
                    stage.load_config(self.write({**self.cfg, key: value}))

    def test_config_file_mode_ownership_symlink_and_duplicates_refused(self):
        with self.assertRaises(stage.StageError):
            stage.load_config(self.write(self.cfg, mode=0o644))
        path = self.write('{"v":1,"v":1}')
        with self.assertRaises(stage.StageError):
            stage.load_config(path)
        real = self.write(self.cfg)
        link = self.base / 'etc/link.json'
        os.symlink(real, link)
        with self.assertRaises(stage.StageError):
            stage.load_config(str(link))
        with patch.object(stage, 'ROOT_UID', self.uid + 1), self.assertRaises(stage.StageError):
            stage.load_config(real)


class CliTests(StageCase):
    def invoke(self, argv=('--stage-current',), identity=(0, 0), entry=None, transport=None, passwd=None):
        out = io.StringIO()
        release = Release()
        rc = stage.run(
            list(argv), identity=identity, entry_file=entry or stage.ENTRY_PATH, stream=out,
            config_loader=lambda: self.cfg,
            transport_factory=lambda base, deadline: transport or release.transport_for(),
            passwd=passwd or (lambda name: types.SimpleNamespace(pw_uid=self.uid, pw_name=name)),
            release_root=self.release_root, profile_path=self.profile, ledger_path=self.ledger)
        return rc, json.loads(out.getvalue())

    def test_stage_current_is_the_only_mode_and_no_authority_arguments(self):
        for argv in ([], ['--url', 'https://evil'], ['--stage-current', '--path', '/x'], ['--stage-current', '--hash', 'a'],
                     ['--stage-current', '--commit', SHA], ['--stage-current', '--pid', '1'], ['--stage-current', '--profile', 'x']):
            with self.subTest(argv=argv), patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
                self.invoke(argv)
        self.assertEqual(self.listing(), [])

    def test_requires_actual_uid_and_euid_zero(self):
        for identity in ((1000, 1000), (0, 1000), (1000, 0)):
            with self.subTest(identity=identity):
                rc, result = self.invoke(identity=identity)
                self.assertEqual((rc, result['reason']), (1, 'root_required'))
        self.assertEqual(self.listing(), [])

    def test_only_fixed_installed_entry_may_run(self):
        rc, result = self.invoke(entry='/home/user/checkout/scripts/hub-natural-stage.py')
        self.assertEqual((rc, result['reason']), (1, 'not_fixed_entry'))
        self.assertEqual(self.listing(), [])

    def test_root_target_user_refused(self):
        rc, result = self.invoke(passwd=lambda name: types.SimpleNamespace(pw_uid=0, pw_name=name))
        self.assertEqual((rc, result['reason']), (1, 'invalid_target_user'))

    def test_success_reports_non_gating_result_and_never_activates(self):
        rc, result = self.invoke()
        self.assertEqual(rc, 0)
        self.assertEqual((result['result'], result['gating']), ('staged', False))

    def test_environment_never_supplies_authority(self):
        env = {'HUB_BASE_URL': 'https://evil.invalid', 'SSL_CERT_FILE': '/tmp/evil.pem',
               'HTTPS_PROXY': 'http://evil.invalid:3128', 'HUB_NATURAL_PROFILE': '/tmp/p'}
        with patch.dict(os.environ, env):
            rc, _ = self.invoke()
        self.assertEqual(rc, 0)


class TransportTests(unittest.TestCase):
    class Response:
        def __init__(self, status=200, body=b'ok', headers=None, chunk=None):
            self.status, self.body, self.headers = status, body, headers or {}
            self.offset = 0

        def getheader(self, name):
            return self.headers.get(name)

        def read(self, amount):
            data = self.body[self.offset:self.offset + amount]
            self.offset += len(data)
            return data

    class Connection:
        def __init__(self, response, log):
            self.response, self.log = response, log

        def request(self, method, path, headers=None):
            self.log.append((method, path, headers))

        def getresponse(self):
            return self.response

        def close(self):
            self.log.append('closed')

    def transport(self, response, log, clock=None):
        def factory(host, port, timeout, context):
            log.append((host, port, context))
            return self.Connection(response, log)
        t = stage.HttpsTransport('https://hub.example', 100.0, monotonic=clock or (lambda: 0.0),
                                 connection_factory=factory)
        t._context = lambda: 'fake-context'
        return t

    def test_exact_origin_path_and_trusted_context(self):
        log = []
        t = self.transport(self.Response(body=b'data', headers={'Content-Length': '4'}), log)
        self.assertEqual(t.get('host-release.json', 100), b'data')
        self.assertEqual(log[0][:2], ('hub.example', 443))
        self.assertEqual(log[0][2], 'fake-context')
        self.assertEqual(log[1][:2], ('GET', '/cli/host-release.json'))
        self.assertEqual(log[-1], 'closed')

    def test_redirects_and_error_statuses_are_rejected_not_followed(self):
        for status in (301, 302, 307, 308, 404, 500):
            with self.subTest(status):
                log = []
                t = self.transport(self.Response(status=status, headers={'Location': 'http://evil.invalid/'}), log)
                with self.assertRaisesRegex(stage.StageError, 'unexpected_http_status'):
                    t.get('hub.pyz', 100)
                self.assertEqual(len([e for e in log if isinstance(e, tuple) and e[0] == 'GET']), 1)

    def test_size_encoding_and_truncation_bounds(self):
        cases = [
            (self.Response(body=b'x' * 10, headers={'Content-Length': '999'}), 'artifact_too_large'),
            (self.Response(body=b'x' * 50), 'artifact_too_large'),
            (self.Response(body=b'x', headers={'Content-Encoding': 'gzip'}), 'unexpected_encoding'),
            (self.Response(body=b'xx', headers={'Content-Length': '5'}), 'artifact_truncated'),
        ]
        for response, reason in cases:
            with self.subTest(reason):
                with self.assertRaisesRegex(stage.StageError, reason):
                    self.transport(response, []).get('hub.pyz', 20)

    def test_original_deadline_is_enforced(self):
        ticks = itertools.chain([0.0, 0.0], itertools.repeat(200.0))
        t = self.transport(self.Response(body=b'x' * 10), [], clock=lambda: next(ticks))
        with self.assertRaisesRegex(stage.StageError, 'deadline_expired'):
            t.get('hub.pyz', 100)

    def test_unsafe_names_refused(self):
        for name in ('../x', 'a/b', '', '.hidden', 'a b'):
            with self.subTest(name), self.assertRaises(stage.StageError):
                self.transport(self.Response(), []).get(name, 10)

    def test_ca_context_uses_fixed_bundle_not_environment(self):
        with patch.object(stage.ssl, 'create_default_context') as create, \
                patch.dict(os.environ, {'SSL_CERT_FILE': '/tmp/evil.pem', 'SSL_CERT_DIR': '/tmp/evil'}):
            context = stage.HttpsTransport('https://hub.example', 1.0, ca_bundle='/etc/ssl/certs/ca-certificates.crt')._context()
        create.assert_called_once_with(cafile='/etc/ssl/certs/ca-certificates.crt')
        self.assertIs(context, create.return_value)

    def test_transport_errors_are_normalised(self):
        def boom(host, port, timeout, context):
            raise OSError('network down')
        t = stage.HttpsTransport('https://hub.example', 100.0, monotonic=lambda: 0.0, connection_factory=boom)
        t._context = lambda: None
        with self.assertRaisesRegex(stage.StageError, 'transport_failure'):
            t.get('hub.pyz', 10)


class NoExecutionTests(unittest.TestCase):
    """The Root helper must contain no import/execution path for foreign code."""

    SOURCE = (REPO / 'scripts/hub-natural-stage.py').read_text()
    TREE = ast.parse(SOURCE)

    def test_imports_are_stdlib_data_modules_only(self):
        imported = set()
        for node in ast.walk(self.TREE):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or '').split('.')[0])
        forbidden = {'subprocess', 'importlib', 'runpy', 'ctypes', 'pickle', 'marshal', 'imp', 'zipimport',
                     'pkgutil', 'multiprocessing', 'socketserver', 'urllib', 'requests', 'agent_hub_host'}
        self.assertEqual(imported & forbidden, set())

    def test_no_dynamic_execution_calls_or_environment_reads(self):
        banned_names = {'exec', 'eval', 'compile', '__import__', 'system', 'popen', 'execv', 'execve', 'execvp',
                        'spawnv', 'fork', 'getenv', 'putenv', 'run_path', 'import_module', 'load_module'}
        for node in ast.walk(self.TREE):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ''
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == 're':
                    continue  # re.compile builds a pattern; it never compiles code
                self.assertNotIn(name, banned_names)
            if isinstance(node, ast.Attribute):
                self.assertNotEqual(node.attr, 'environ')
        self.assertNotIn('sys.path', self.SOURCE)

    def test_archive_bytes_are_only_read_never_loaded(self):
        # A hostile archive whose __main__ would fail loudly is only ever parsed as data.
        release = Release(archive_tweak=lambda m: [(n, 'raise SystemExit("executed!")\n' if n == 'agent_hub_host/agent.py' else d) for n, d in m])
        with self.assertRaises(stage.StageError):
            stage.validate_archive(release.archive, manifest=release.manifest(), host_wheel_name=HOST_WHEEL,
                                   wheel_bytes=release.wheel, transport_bytes=release.transport, payloads=PAYLOAD_BYTES)


if __name__ == '__main__':
    unittest.main()
