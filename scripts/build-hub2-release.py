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
import shutil
import subprocess
import sys
import tarfile
import tempfile

REPO = Path(__file__).resolve().parent.parent
UNITS = ('agent-hub2-host.service', 'agent-hub2-host-update.service', 'agent-hub2-host-update.timer')


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
                   'deploy/agent-hub2-host-update.timer', 'deploy/host-install.sh'])
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
        project.write_text(project.read_text().replace('version = "2.0.0"', f'version = "2.0.0+g{sha}"'))
        artifacts = Path(temporary) / 'artifacts'
        artifacts.mkdir()
        run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--wheel-dir', str(artifacts), str(project.parent)])
        # Pure Python transport wheel installs on all supported host platforms.
        run([sys.executable, '-m', 'pip', 'download', '--only-binary=:all:', '--no-deps',
             '--platform', 'any', '--implementation', 'py', '--abi', 'none',
             '--dest', str(artifacts), 'websockets==16.0'])
        spec = importlib.util.spec_from_file_location('hub_cli_release_builder', source / 'scripts/build-hub-cli.py')
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        cli.git_sha = lambda: sha
        cli.build(artifacts, '2.0.0+g' + sha)
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
        # Publish manifest last: interrupted downloads fail hashes and retry next tick.
        for path in artifacts.iterdir():
            shutil.copyfile(path, output / path.name)
        pending = output / '.host-release.json.tmp'
        pending.write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        pending.replace(output / 'host-release.json')
        return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rev', default='HEAD')
    parser.add_argument('--out-dir', type=Path, default=REPO / 'hub2/public/cli')
    args = parser.parse_args()
    manifest = build(REPO, args.rev, args.out_dir.resolve())
    print('Built Hub2 release ' + manifest['host_version'])


if __name__ == '__main__':
    main()
