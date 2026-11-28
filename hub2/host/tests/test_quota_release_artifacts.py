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
        'websockets/__init__.py': b'# websockets/__init__.py\n',
        'websockets/asyncio/__init__.py': b'',
        'websockets/exceptions.py': b'# websockets/exceptions.py\n',
        'websockets/sync/client.py': b'# websockets/sync/client.py\n',
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
    for name in dispatcher.NATURAL_PAYLOADS:
        (root / name).write_bytes(natural_sources[name])
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
        self.assertLessEqual(len(verified['artifacts']), 20)
        with zipfile.ZipFile(bundle / builder.NATURAL_ARCHIVE) as zf:
            inner = json.loads(zf.read('MANIFEST.json').decode('utf-8'))
            self.assertEqual(zf.read('websockets/asyncio/__init__.py'), b'')
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
        for name in (builder.NATURAL_ARCHIVE, 'hub-quota-publish.py', 'natural-quota-prelude.py'):
            (bundle / name).unlink()
        manifest = json.loads((bundle / 'host-release.json').read_bytes())
        for name in (builder.NATURAL_ARCHIVE, 'hub-quota-publish.py', 'natural-quota-prelude.py'):
            del manifest['artifacts'][name]
        raw = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
        (bundle / 'host-release.json').write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        verified = builder.verify_bundle(bundle, COMMIT, digest)
        self.assertNotIn(builder.NATURAL_ARCHIVE, verified['artifacts'])

    def test_tampered_or_self_authorized_natural_archive_is_rejected(self):
        def _subst_payload(name):
            def _mutate(m, i):
                bad = b'# substituted ' + name.encode() + b'\n'
                m['natural/' + name] = bad
                i['host_release']['natural_payloads'][name] = hashlib.sha256(bad).hexdigest()
            return _mutate

        cases = [
            ('substituted_host_module', lambda m, i: m.__setitem__('agent_hub_host/agent.py', b'# evil\n'),
             'host module differs from outer wheel'),
            ('substituted_transport_module', lambda m, i: m.__setitem__('websockets/sync/client.py', b'# evil\n'),
             'transport module differs from outer wheel'),
            ('substituted_entrypoint', lambda m, i: m.__setitem__('__main__.py', b'raise SystemExit(0)\n'),
             'entrypoint differs from maintained stub'),
            ('substituted_publish_payload', _subst_payload('hub-quota-publish.py'),
             'payload differs from outer bundle'),
            ('substituted_prelude_payload', _subst_payload('natural-quota-prelude.py'),
             'payload differs from outer bundle'),
            ('tampered_alert_client', lambda m, i: m.__setitem__('natural/hub_alert_client.py', b'tampered\n'),
             'payload checksum mismatch|payload differs from outer bundle'),
            ('extra_member', lambda m, i: m.__setitem__('agent_hub_host/extra.py', b'# extra\n'),
             'members differ from outer verified inputs'),
            ('mixed_commit_marker', lambda m, i: m.__setitem__('agent_hub_host/_release.py',
             f'HOST_VERSION = {"d" * 40!r}\nPROTOCOL_VERSION = 1\n'.encode()),
             'host module differs from outer wheel|release marker mismatch'),
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
        for name in (builder.NATURAL_ARCHIVE, 'hub-quota-publish.py', 'natural-quota-prelude.py'):
            (bundle / name).unlink()
        manifest = json.loads((bundle / 'host-release.json').read_bytes())
        for name in (builder.NATURAL_ARCHIVE, 'hub-quota-publish.py', 'natural-quota-prelude.py'):
            del manifest['artifacts'][name]
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
