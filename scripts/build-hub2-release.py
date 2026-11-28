#!/usr/bin/env python3
"""Build a matched host wheel and CLI from a committed source snapshot.

Run in CI, never in the live host environment. Output is served at /cli/.
The wheel build backend and transport versions come from the pinned pyproject.
"""
import argparse
import hashlib
import importlib.util
import io
import json
from pathlib import Path, PurePosixPath
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

REPO = Path(__file__).resolve().parent.parent
UNITS = ('agent-hub2-host.service', 'agent-hub2-host-update.service', 'agent-hub2-host-update.timer')
CLI_INSTALLERS = ('install.sh', 'install.ps1')
# Windows host installer/updater (intake 41ef7048), served at /cli/host-install.ps1.
WINDOWS_HOST_INSTALLER = 'host-install.ps1'
WINDOWS_READER_CLOSURE = ('private_fs.py', 'win32_fs.py')
WINDOWS_ARTIFACT_ABI = 'hub2-windows-credential-artifacts-v1'
NATURAL_ARCHIVE = 'agent-hub-quota-dispatcher.pyz'
NATURAL_PAYLOADS = ('hub-quota-publish.py', 'hub_alert_client.py', 'natural-quota-prelude.py')
NATURAL_MAIN_STUB = (
    'from agent_hub_host.quota_dispatch_entry import main\n'
    'raise SystemExit(main())\n'
)
NATURAL_HOST_CLOSURE = (
    '__init__.py', 'alert_authority.py', 'alert_broker.py', 'alert_collector.py',
    'quota_dispatch.py', 'quota_dispatch_manager.py', 'quota_dispatch_entry.py',
    'quota_natural_primary.py', 'quota_natural_resources.py', 'quota_natural_startup.py',
    'agent.py', 'cli.py', 'transport.py', '_release.py',
)
MAX_MANIFEST = 128 * 1024


def read_manifest(path, expected_sha256=None):
    if path.is_symlink() or not path.is_file():
        raise ValueError('release manifest is not a regular file')
    with path.open('rb') as stream:
        raw = stream.read(MAX_MANIFEST + 1)
    if len(raw) > MAX_MANIFEST:
        raise ValueError('release manifest exceeds size limit')
    if expected_sha256 is not None and hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('release manifest differs from the CI bundle')
    return json.loads(raw)


def embedded_marker(archive_path, name):
    with zipfile.ZipFile(archive_path) as archive:
        markers = [entry for entry in archive.infolist() if entry.filename == name]
        if len(markers) != 1 or not 0 < markers[0].file_size <= 4096:
            raise ValueError('invalid embedded release marker')
        return archive.read(markers[0]).decode('utf-8')


def verify_bundle(directory, expected_commit, expected_manifest_sha256):
    if not re.fullmatch(r'[0-9a-f]{40}', expected_commit) or not re.fullmatch(r'[0-9a-f]{64}', expected_manifest_sha256):
        raise ValueError('verification requires a full commit and CI manifest SHA-256')
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('release directory is not a real directory')
    manifest = read_manifest(directory / 'host-release.json', expected_manifest_sha256)
    validate = runpy.run_path(str(REPO / 'scripts/hub2-self-update.py'))['validate_manifest']
    wheel = validate(manifest)
    if manifest['host_version'] != expected_commit:
        raise ValueError('release does not match the deployment commit')

    records = manifest['artifacts']
    expected_files = {'hub.pyz', 'hub.pyz.sha256', 'manifest.json', 'host-install.sh',
                      'hub2-self-update.py', 'hub2-enroll.py', 'websockets-16.0-py3-none-any.whl',
                      wheel, *CLI_INSTALLERS, *UNITS}
    # Retained pre-client releases remain valid rollback targets under their
    # original CI-pinned manifest. New builds always emit the standalone client.
    if 'hub_alert_client.py' in records:
        expected_files.add('hub_alert_client.py')
    # Releases built before the Windows host installer stay valid rollback targets.
    if WINDOWS_HOST_INSTALLER in records:
        expected_files.add(WINDOWS_HOST_INSTALLER)
    # Retained pre-natural-archive releases remain valid rollback targets.
    for name in ('hub-quota-publish.py', 'natural-quota-prelude.py', NATURAL_ARCHIVE):
        if name in records:
            expected_files.add(name)
    if records.keys() != expected_files or {path.name for path in directory.iterdir()} != expected_files | {'host-release.json'}:
        raise ValueError('release directory has missing or unexpected files')
    for name, record in records.items():
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != record['size']:
            raise ValueError('release artifact type or size mismatch: ' + name)
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != record['sha256']:
                raise ValueError('release artifact checksum mismatch: ' + name)

    cli = read_manifest(directory / 'manifest.json')
    cli_digest = records['hub.pyz']['sha256']
    if not isinstance(cli, dict) or cli.get('built_from_commit') != expected_commit or cli.get('sha256') != cli_digest:
        raise ValueError('CLI manifest does not match the bundle')
    if (directory / 'hub.pyz.sha256').read_bytes() != f'{cli_digest}  hub.pyz\n'.encode():
        raise ValueError('CLI checksum file does not match the bundle')
    cli_marker = embedded_marker(directory / 'hub.pyz', 'agent_hub_host/_build_info.py')
    if not re.search(rf'^BUILD_COMMIT = "{expected_commit}"$', cli_marker, re.MULTILINE):
        raise ValueError('CLI build does not match the deployment commit')
    wheel_marker = embedded_marker(directory / wheel, 'agent_hub_host/_release.py')
    expected = f'HOST_VERSION = {expected_commit!r}\nPROTOCOL_VERSION = {manifest["protocol_version"]}\n'
    if wheel_marker != expected:
        raise ValueError('host wheel does not match the deployment commit and protocol')
    if NATURAL_ARCHIVE in records:
        verify_natural_archive(directory, expected_commit, expected, records, wheel)
    return manifest


def _safe_zip_member(entry):
    name = entry.filename
    path = PurePosixPath(name)
    if (path.is_absolute() or '..' in path.parts or '\\' in name
            or name.rstrip('/') != str(path)
            or any(ord(char) < 32 for char in name)
            or stat.S_ISLNK(entry.external_attr >> 16)):
        raise ValueError('unsafe archive member')


def _wheel_python_hashes(wheel_path, package):
    """Independently read and hash `<package>/*.py` members from a verified outer wheel."""
    hashes = {}
    total = 0
    with zipfile.ZipFile(wheel_path) as wheel:
        entries = wheel.infolist()
        names = [entry.filename for entry in entries]
        if len(entries) > 512 or len(set(names)) != len(names):
            raise ValueError('outer wheel member ambiguity')
        for entry in entries:
            _safe_zip_member(entry)
            name = entry.filename
            if entry.is_dir() or not name.startswith(package + '/') or not name.endswith('.py'):
                continue
            total += entry.file_size
            if not 0 <= entry.file_size <= 1024 * 1024 or total > 16 * 1024 * 1024:
                raise ValueError('outer wheel source size')
            hashes[name] = hashlib.sha256(wheel.read(entry)).hexdigest()
    if package + '/__init__.py' not in hashes:
        raise ValueError('outer wheel package missing')
    return hashes


def verify_natural_archive(directory, expected_commit, expected_release_marker, records, wheel_name):
    """Verify inert natural Host archive against outer verified wheels and payloads without execution."""
    for name in NATURAL_PAYLOADS:
        if name not in records:
            raise ValueError('missing outer natural payload record: ' + name)
    outer_host = _wheel_python_hashes(directory / wheel_name, 'agent_hub_host')
    outer_transport = _wheel_python_hashes(directory / 'websockets-16.0-py3-none-any.whl', 'websockets')
    with zipfile.ZipFile(directory / NATURAL_ARCHIVE) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(entries) > 512 or len(set(names)) != len(names):
            raise ValueError('natural archive member ambiguity')
        actual_hashes = {}
        total = 0
        for entry in entries:
            _safe_zip_member(entry)
            if entry.is_dir():
                continue
            total += entry.file_size
            if not 0 <= entry.file_size <= 1024 * 1024 or total > 16 * 1024 * 1024:
                raise ValueError('natural archive member size')
            actual_hashes[entry.filename] = hashlib.sha256(archive.read(entry)).hexdigest()
        if 'MANIFEST.json' not in actual_hashes or '__main__.py' not in actual_hashes:
            raise ValueError('natural archive missing manifest or entrypoint')
        inner = json.loads(archive.read('MANIFEST.json').decode('utf-8'))
        if (not isinstance(inner, dict) or inner.get('v') != 1
                or inner.get('entry') != 'agent_hub_host.quota_dispatch_entry:main'
                or not isinstance(inner.get('sources'), dict)
                or not isinstance(inner.get('host_release'), dict)):
            raise ValueError('invalid natural archive manifest')
        sources = inner['sources']
        expected_members = set(actual_hashes) - {'MANIFEST.json'}
        if set(sources.keys()) != expected_members:
            raise ValueError('natural archive manifest member mismatch')
        for name, digest in sources.items():
            if actual_hashes[name] != digest:
                raise ValueError('natural archive member checksum mismatch: ' + name)
        required = {'__main__.py', 'websockets/__init__.py', 'websockets/exceptions.py',
                    'websockets/sync/client.py',
                    *(f'agent_hub_host/{name}' for name in NATURAL_HOST_CLOSURE),
                    *(f'natural/{name}' for name in NATURAL_PAYLOADS)}
        if not required <= expected_members:
            raise ValueError('natural archive lacks required closure')
        expected_exact = {'__main__.py', *outer_host, *outer_transport,
                          *(f'natural/{name}' for name in NATURAL_PAYLOADS)}
        if expected_members != expected_exact:
            raise ValueError('natural archive members differ from outer verified inputs')
        for name, digest in outer_host.items():
            if actual_hashes[name] != digest:
                raise ValueError('natural archive host module differs from outer wheel: ' + name)
        for name, digest in outer_transport.items():
            if actual_hashes[name] != digest:
                raise ValueError('natural archive transport module differs from outer wheel: ' + name)
        if archive.read('__main__.py').decode('utf-8') != NATURAL_MAIN_STUB:
            raise ValueError('natural archive entrypoint differs from maintained stub')
        host_release = inner['host_release']
        if host_release.get('commit') != expected_commit:
            raise ValueError('natural archive does not match the deployment commit')
        if host_release.get('host_wheel_sha256') != records[wheel_name]['sha256']:
            raise ValueError('natural archive host wheel binding mismatch')
        if host_release.get('transport_wheel_sha256') != records['websockets-16.0-py3-none-any.whl']['sha256']:
            raise ValueError('natural archive transport wheel binding mismatch')
        payload_hashes = host_release.get('natural_payloads')
        if (not isinstance(payload_hashes, dict)
                or set(payload_hashes.keys()) != set(NATURAL_PAYLOADS)):
            raise ValueError('natural archive payload provenance invalid')
        for name in NATURAL_PAYLOADS:
            if payload_hashes[name] != actual_hashes['natural/' + name]:
                raise ValueError('natural archive payload checksum mismatch: ' + name)
            if actual_hashes['natural/' + name] != records[name]['sha256']:
                raise ValueError('natural archive payload differs from outer bundle: ' + name)
        if archive.read('agent_hub_host/_release.py').decode('utf-8') != expected_release_marker:
            raise ValueError('natural archive release marker mismatch')


def run(args, **kwargs):
    # Build tools may print repository URLs; expose only the stage on failure.
    result = subprocess.run(args, capture_output=True, timeout=300, **kwargs)
    if result.returncode:
        raise RuntimeError('release build command failed: ' + Path(args[0]).name)
    return result.stdout


def export_source(repo, revision, target):
    sha = run(['git', '-C', str(repo), 'rev-parse', revision + '^{commit}']).decode().strip()
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise ValueError('invalid source revision')
    archive = run(['git', '-C', str(repo), 'archive', sha, '--', 'hub2/host', 'host/agent_hub_host',
                   'config/model-routing.json', 'scripts/build-hub-cli.py',
                   'scripts/session-host-supervisor.py', 'scripts/hub2-self-update.py', 'scripts/hub2-enroll.py',
                   'scripts/build-quota-dispatcher.py', *(f'scripts/{name}' for name in NATURAL_PAYLOADS),
                   'deploy/agent-hub2-host.service', 'deploy/agent-hub2-host-update.service',
                   'deploy/agent-hub2-host-update.timer', 'deploy/host-install.sh', 'deploy/' + WINDOWS_HOST_INSTALLER, 'uv.lock',
                   *(f'public/cli/{name}' for name in CLI_INSTALLERS)])
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for entry in bundle:
            name = Path(entry.name)
            if name.is_absolute() or '..' in name.parts or entry.issym() or entry.islnk():
                raise ValueError('unsafe source entry')
            # Exclude tracked stale build output as well as bytecode and metadata.
            if any(part in ('build', '__pycache__') or part.endswith('.egg-info') for part in name.parts):
                continue
            dest = target / name
            if entry.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            elif entry.isfile():
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(bundle.extractfile(entry).read())
            else:
                raise ValueError('unsupported source entry')
    return sha


def prepare_windows_reader(package, host_source):
    """The wheel verifier closure and zip helper originate in one Git snapshot."""
    for name in WINDOWS_READER_CLOSURE:
        if (package / name).read_bytes() != (host_source / name).read_bytes():
            raise ValueError('Windows safe-reader closure differs from maintained source')
    helper_sha = hashlib.sha256((host_source / 'managed_claude_deliver.py').read_bytes()).hexdigest()
    consumer_sha = hashlib.sha256((host_source / 'windows_managed_claude_deliver.py').read_bytes()).hexdigest()
    (package / '_credential_artifact_binding.py').write_text(
        f'HELPER_SOURCE_SHA256 = {helper_sha!r}\nHELPER_ABI = {WINDOWS_ARTIFACT_ABI!r}\n'
        f'WINDOWS_CONSUMER_SOURCE_SHA256 = {consumer_sha!r}\n'
        "WINDOWS_CONSUMER_ABI = 'hub2-windows-managed-claude-consumer-v1'\n")


def build(repo, revision, output):
    scratch = repo / '.local/tmp'
    scratch.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='hub2-release-', dir=scratch) as temporary:
        source = Path(temporary) / 'source'
        source.mkdir()
        sha = export_source(repo, revision, source)
        package = source / 'hub2/host/agent_hub_host'
        # Normal source imports and wheels use the same maintained reader bytes.
        # Do not bootstrap archive verification by importing an unverified zip.
        prepare_windows_reader(package, source / 'host/agent_hub_host')
        (package / '_release.py').write_text(f'HOST_VERSION = {sha!r}\nPROTOCOL_VERSION = 1\n')
        # Distinct wheel versions avoid pip treating different commits as 2.0.0.
        project = source / 'hub2/host/pyproject.toml'
        contents = project.read_text()
        if contents.count('version = "2.0.0"') != 1:
            raise ValueError('host package version changed; update the reviewed release contract')
        project.write_text(contents.replace('version = "2.0.0"', f'version = "2.0.0+g{sha}"'))
        artifacts = Path(temporary) / 'artifacts'
        artifacts.mkdir()
        run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--wheel-dir', str(artifacts), str(project.parent)])
        # Pure Python transport wheel installs on all supported host platforms.
        lock = tomllib.loads((source / 'uv.lock').read_text())
        package_record = next(item for item in lock['package'] if item['name'] == 'websockets' and item['version'] == '16.0')
        wheel_record = next(item for item in package_record['wheels'] if item['url'].endswith('/websockets-16.0-py3-none-any.whl'))
        requirements = Path(temporary) / 'transport.txt'
        requirements.write_text('websockets==16.0 --hash=' + wheel_record['hash'] + '\n')
        run([sys.executable, '-m', 'pip', 'download', '--only-binary=:all:', '--no-deps',
             '--platform', 'any', '--implementation', 'py', '--abi', 'none',
             '--require-hashes', '--dest', str(artifacts), '--requirement', str(requirements)])
        spec = importlib.util.spec_from_file_location('hub_cli_release_builder', source / 'scripts/build-hub-cli.py')
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        cli.git_sha = lambda: sha
        cli.build(artifacts, '2.0.0+g' + sha)
        for installer in CLI_INSTALLERS:
            shutil.copyfile(source / 'public/cli' / installer, artifacts / installer)
        shutil.copyfile(source / 'scripts/hub2-self-update.py', artifacts / 'hub2-self-update.py')
        shutil.copyfile(source / 'scripts/hub2-enroll.py', artifacts / 'hub2-enroll.py')
        for name in NATURAL_PAYLOADS:
            shutil.copyfile(source / 'scripts' / name, artifacts / name)
        shutil.copyfile(source / 'deploy/host-install.sh', artifacts / 'host-install.sh')
        shutil.copyfile(source / 'deploy' / WINDOWS_HOST_INSTALLER, artifacts / WINDOWS_HOST_INSTALLER)
        for unit in UNITS:
            shutil.copyfile(source / 'deploy' / unit, artifacts / unit)
        host_wheels = [path.name for path in artifacts.iterdir()
                       if path.name.startswith('agent_hub_host_v2-') and path.name.endswith('.whl')]
        if len(host_wheels) != 1:
            raise ValueError('host wheel ambiguity during natural archive build')
        disp_spec = importlib.util.spec_from_file_location(
            'hub_quota_dispatcher_builder', source / 'scripts/build-quota-dispatcher.py')
        dispatcher = importlib.util.module_from_spec(disp_spec)
        disp_spec.loader.exec_module(dispatcher)
        dispatcher.build_normal_host_archive(
            artifacts / NATURAL_ARCHIVE,
            host_wheel_path=artifacts / host_wheels[0],
            transport_wheel_path=artifacts / 'websockets-16.0-py3-none-any.whl',
            commit=sha,
            uv_lock_bytes=(source / 'uv.lock').read_bytes(),
            natural_sources={name: (source / 'scripts' / name).read_bytes() for name in NATURAL_PAYLOADS},
        )
        files = {}
        for path in sorted(artifacts.iterdir()):
            files[path.name] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'size': path.stat().st_size}
        manifest = {'schema_version': 1, 'host_version': sha, 'protocol_version': 1,
                    'python_requires': '>=3.11', 'artifacts': files}
        manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
        (artifacts / 'host-release.json').write_bytes(manifest_bytes)
        verify_bundle(artifacts, sha, hashlib.sha256(manifest_bytes).hexdigest())
        # Publish manifest last: interrupted downloads fail hashes and retry next tick.
        for name in files:
            shutil.copyfile(artifacts / name, output / name)
        pending = output / '.host-release.json.tmp'
        pending.write_bytes(manifest_bytes)
        pending.replace(output / 'host-release.json')
        return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rev', default='HEAD')
    parser.add_argument('--out-dir', type=Path, default=REPO / 'hub2/public/cli')
    parser.add_argument('--verify-dir', type=Path, help='Verify a staged release instead of building')
    parser.add_argument('--manifest-sha256', help='Expected host-release.json digest from CI')
    args = parser.parse_args()
    if args.verify_dir is not None:
        if args.manifest_sha256 is None:
            parser.error('--verify-dir requires --manifest-sha256 and a full commit in --rev')
        try:
            verify_bundle(args.verify_dir, args.rev, args.manifest_sha256)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            parser.exit(1, f'Hub2 release verification failed: {exc}\n')
        print('Verified Hub2 release ' + args.rev)
        return
    if args.manifest_sha256 is not None:
        parser.error('--manifest-sha256 requires --verify-dir')
    manifest = build(REPO, args.rev, args.out_dir.resolve())
    print('Built Hub2 release ' + manifest['host_version'])


if __name__ == '__main__':
    main()
