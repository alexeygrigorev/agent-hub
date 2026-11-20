"""Tests for inert natural Host release archive assembly and verification."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

REPO = Path(__file__).resolve().parents[3]


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


builder = _load('build_hub2_release_for_quota_tests', 'scripts/build-hub2-release.py')
dispatcher = _load('build_quota_dispatcher_for_quota_tests', 'scripts/build-quota-dispatcher.py')
COMMIT = 'c' * 40
HOST_WHEEL = f'agent_hub_host_v2-2.0.0+g{COMMIT}-py3-none-any.whl'
TRANSPORT_WHEEL = 'websockets-16.0-py3-none-any.whl'


def _build_wheel(path, entries):
    with zipfile.ZipFile(path, 'w') as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def _synthetic_bundle(root):
    root.mkdir(parents=True, exist_ok=True)
    host_entries = {
        f'agent_hub_host/{name}': (
            f'HOST_VERSION = {COMMIT!r}\nPROTOCOL_VERSION = 1\n'.encode()
            if name == '_release.py' else f'# {name}\n'.encode()
        )
        for name in builder.NATURAL_HOST_CLOSURE
    }
    _build_wheel(root / HOST_WHEEL, host_entries)
    transport_entries = {
        name: f'# {name}\n'.encode()
        for name in ('websockets/__init__.py', 'websockets/exceptions.py', 'websockets/sync/client.py')
    }
    _build_wheel(root / TRANSPORT_WHEEL, transport_entries)
    transport_bytes = (root / TRANSPORT_WHEEL).read_bytes()
    uv_lock = (
        '[[package]]\nname = "websockets"\nversion = "16.0"\n'
        f'wheels = [{{url = "https://fixture.invalid/{TRANSPORT_WHEEL}", '
        f'hash = "sha256:{hashlib.sha256(transport_bytes).hexdigest()}", '
        f'size = {len(transport_bytes)}}}]\n'
    ).encode()
    natural_sources = {
        name: f'# synthetic {name}\n'.encode()
        for name in dispatcher.NATURAL_PAYLOADS
    }
    dispatcher.build_normal_host_archive(
        root / builder.NATURAL_ARCHIVE,
        host_wheel_path=root / HOST_WHEEL,
        transport_wheel_path=root / TRANSPORT_WHEEL,
        commit=COMMIT,
        uv_lock_bytes=uv_lock,
        natural_sources=natural_sources,
    )
    with zipfile.ZipFile(root / 'hub.pyz', 'w') as zf:
        zf.writestr('agent_hub_host/_build_info.py', f'BUILD_COMMIT = "{COMMIT}"\n')
        zf.writestr('__main__.py', 'raise RuntimeError("never executed")\n')
    cli_sha = hashlib.sha256((root / 'hub.pyz').read_bytes()).hexdigest()
    (root / 'hub.pyz.sha256').write_text(f'{cli_sha}  hub.pyz\n')
    (root / 'manifest.json').write_text(json.dumps({'built_from_commit': COMMIT, 'sha256': cli_sha}))
    (root / 'hub_alert_client.py').write_bytes(natural_sources['hub_alert_client.py'])
    for name in ('host-install.sh', builder.WINDOWS_HOST_INSTALLER, 'hub2-self-update.py',
                 'hub2-enroll.py', *builder.CLI_INSTALLERS, *builder.UNITS):
        (root / name).write_bytes(b'synthetic\n')
    records = {
        path.name: {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'size': path.stat().st_size}
        for path in sorted(root.iterdir())
    }
    manifest = {
        'schema_version': 1, 'host_version': COMMIT, 'protocol_version': 1,
        'python_requires': '>=3.11', 'artifacts': records,
    }
    raw = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    (root / 'host-release.json').write_bytes(raw)
    return root, hashlib.sha256(raw).hexdigest(), uv_lock, natural_sources


def _repack_with_manifest(archive_path, bundle_dir, mutator):
    with zipfile.ZipFile(archive_path) as src:
        members = {entry.filename: src.read(entry) for entry in src.infolist() if not entry.is_dir()}
    inner = json.loads(members['MANIFEST.json'].decode('utf-8'))
    mutator(members, inner)
    inner['sources'] = {
        name: hashlib.sha256(data).hexdigest()
        for name, data in members.items() if name != 'MANIFEST.json'
    }
    members['MANIFEST.json'] = json.dumps(inner, sort_keys=True, separators=(',', ':')).encode()
    archive_path.unlink()
    with zipfile.ZipFile(archive_path, 'w') as dst:
        for name, data in sorted(members.items()):
            dst.writestr(name, data)
    manifest = json.loads((bundle_dir / 'host-release.json').read_bytes())
    raw_archive = archive_path.read_bytes()
    manifest['artifacts'][builder.NATURAL_ARCHIVE] = {
        'sha256': hashlib.sha256(raw_archive).hexdigest(),
        'size': len(raw_archive),
    }
    raw_manifest = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    (bundle_dir / 'host-release.json').write_bytes(raw_manifest)
    return hashlib.sha256(raw_manifest).hexdigest()


class NaturalHostReleaseArtifactTests(unittest.TestCase):
    def setUp(self):
        scratch = REPO / '.local/tmp'
        scratch.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=scratch, prefix='test-nat-release-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_normal_archive_is_deterministic_and_verifies_without_circular_manifest(self):
        bundle, digest, uv_lock, natural_sources = _synthetic_bundle(self.root / 'bundle')
        verified = builder.verify_bundle(bundle, COMMIT, digest)
        self.assertIn(builder.NATURAL_ARCHIVE, verified['artifacts'])
        with zipfile.ZipFile(bundle / builder.NATURAL_ARCHIVE) as zf:
            inner = json.loads(zf.read('MANIFEST.json').decode('utf-8'))
        self.assertNotIn('manifest_sha256', inner['host_release'])
        self.assertEqual(inner['host_release']['commit'], COMMIT)
        second = dispatcher.build_normal_host_archive(
            self.root / 'second.pyz',
            host_wheel_path=bundle / HOST_WHEEL,
            transport_wheel_path=bundle / TRANSPORT_WHEEL,
            commit=COMMIT,
            uv_lock_bytes=uv_lock,
            natural_sources=natural_sources,
        )
        self.assertEqual(second['sha256'], verified['artifacts'][builder.NATURAL_ARCHIVE]['sha256'])
        self.assertEqual((self.root / 'second.pyz').read_bytes(), (bundle / builder.NATURAL_ARCHIVE).read_bytes())

    def test_legacy_bundle_without_natural_archive_remains_valid(self):
        bundle, _, _, _ = _synthetic_bundle(self.root / 'legacy')
        (bundle / builder.NATURAL_ARCHIVE).unlink()
        manifest = json.loads((bundle / 'host-release.json').read_bytes())
        del manifest['artifacts'][builder.NATURAL_ARCHIVE]
        raw = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
        (bundle / 'host-release.json').write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        verified = builder.verify_bundle(bundle, COMMIT, digest)
        self.assertNotIn(builder.NATURAL_ARCHIVE, verified['artifacts'])

    def test_tampered_or_self_authorized_natural_archive_is_rejected(self):
        cases = [
            ('tampered_alert_client', lambda m, i: m.__setitem__('natural/hub_alert_client.py', b'tampered\n'),
             'payload checksum mismatch|alert client differs'),
            ('mixed_commit_marker', lambda m, i: m.__setitem__('agent_hub_host/_release.py',
             f'HOST_VERSION = {"d" * 40!r}\nPROTOCOL_VERSION = 1\n'.encode()),
             'release marker mismatch'),
            ('mixed_commit_provenance', lambda m, i: i['host_release'].__setitem__('commit', 'd' * 40),
             'deployment commit'),
            ('wrong_host_wheel_binding', lambda m, i: i['host_release'].__setitem__('host_wheel_sha256', '0' * 64),
             'host wheel binding mismatch'),
            ('missing_closure_member', lambda m, i: m.pop('websockets/sync/client.py'),
             'lacks required closure'),
        ]
        for label, mutator, expected_err in cases:
            with self.subTest(case=label):
                bundle, _, _, _ = _synthetic_bundle(self.root / label)
                new_digest = _repack_with_manifest(bundle / builder.NATURAL_ARCHIVE, bundle, mutator)
                with self.assertRaisesRegex(ValueError, expected_err):
                    builder.verify_bundle(bundle, COMMIT, new_digest)

    def test_externally_pinned_build_host_archive_remains_compatible(self):
        bundle, _, uv_lock, _ = _synthetic_bundle(self.root / 'pinned')
        (bundle / builder.NATURAL_ARCHIVE).unlink()
        manifest = json.loads((bundle / 'host-release.json').read_bytes())
        del manifest['artifacts'][builder.NATURAL_ARCHIVE]
        raw = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
        (bundle / 'host-release.json').write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()

        def fake_committed(_commit, path):
            if path == 'uv.lock':
                return uv_lock
            if path.startswith('scripts/'):
                return b'# committed\n'
            raise AssertionError(path)

        with patch.object(dispatcher, '_committed', side_effect=fake_committed):
            res = dispatcher.build_host_archive(self.root / 'external.pyz', bundle, COMMIT, digest)
        self.assertEqual(res['host_release']['manifest_sha256'], digest)


if __name__ == '__main__':
    unittest.main()
