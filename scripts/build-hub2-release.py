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
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile

REPO = Path(__file__).resolve().parent.parent
UNITS = ('agent-hub2-host.service', 'agent-hub2-host-update.service', 'agent-hub2-host-update.timer')
CLI_INSTALLERS = ('install.sh', 'install.ps1')
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
    return manifest


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
                   'deploy/agent-hub2-host.service', 'deploy/agent-hub2-host-update.service',
                   'deploy/agent-hub2-host-update.timer', 'deploy/host-install.sh', 'uv.lock',
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


def build(repo, revision, output):
    scratch = repo / '.local/tmp'
    scratch.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='hub2-release-', dir=scratch) as temporary:
        source = Path(temporary) / 'source'
        source.mkdir()
        sha = export_source(repo, revision, source)
        package = source / 'hub2/host/agent_hub_host'
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
        shutil.copyfile(source / 'deploy/host-install.sh', artifacts / 'host-install.sh')
        for unit in UNITS:
            shutil.copyfile(source / 'deploy' / unit, artifacts / unit)
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
