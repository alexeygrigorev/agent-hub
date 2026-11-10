#!/usr/bin/env python3
"""Install verified Hub2 releases without modifying a running environment.

The manifest is trusted through HTTPS, not a signature. A SHA check protects
against partial/mixed downloads; it does not replace transport authentication.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import urlopen

UNITS = ('agent-hub2-host.service', 'agent-hub2-host-update.service', 'agent-hub2-host-update.timer')
MAX_MANIFEST = 128 * 1024
MAX_ARTIFACT = 32 * 1024 * 1024


def fetch(url, limit):
    if urlsplit(url).scheme != 'https':
        raise ValueError('release URL requires HTTPS')
    with urlopen(url, timeout=30) as response:
        if urlsplit(response.url).scheme != 'https':
            raise ValueError('release redirect requires HTTPS')
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('release artifact exceeds size limit')
    return data


def validate_manifest(manifest):
    if (not isinstance(manifest, dict) or manifest.get('schema_version') != 1
            or type(manifest.get('protocol_version')) is not int or manifest['protocol_version'] < 1):
        raise ValueError('unsupported release protocol')
    requirement = re.fullmatch(r'>=(\d+)\.(\d+)', str(manifest.get('python_requires', '')))
    if not requirement or sys.version_info[:2] < tuple(map(int, requirement.groups())):
        raise ValueError('release requires a newer or unsupported Python runtime')
    version = manifest.get('host_version')
    if not isinstance(version, str) or not re.fullmatch(r'[0-9a-f]{40}', version):
        raise ValueError('invalid host version')
    files = manifest.get('artifacts')
    if not isinstance(files, dict) or not 6 <= len(files) <= 20:
        raise ValueError('invalid release artifact list')
    for name, record in files.items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]*', name) or name in ('.', '..'):
            raise ValueError('unsafe artifact name')
        if (not isinstance(record, dict) or not isinstance(record.get('sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', record['sha256'])):
            raise ValueError('invalid artifact checksum')
        if type(record.get('size')) is not int or not 0 < record['size'] <= MAX_ARTIFACT:
            raise ValueError('invalid artifact size')
    required = {'hub.pyz', 'hub2-self-update.py', 'hub2-enroll.py', 'host-install.sh', *UNITS}
    if not required <= files.keys():
        raise ValueError('release is incomplete')
    host = [name for name in files if re.fullmatch(r'agent_hub_host_v2-2\.0\.0\+g[0-9a-f]{40}-py3-none-any\.whl', name)]
    if len(host) != 1 or version not in host[0] or 'websockets-16.0-py3-none-any.whl' not in files:
        raise ValueError('release wheel missing or mismatched')
    return host[0]


def command(args):
    result = subprocess.run([str(value) for value in args], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=120)
    if result.returncode:
        raise RuntimeError('release command failed: ' + Path(args[0]).name)


def link(target, path):
    if path.exists() and not path.is_symlink():
        raise ValueError('refusing to replace an unmanaged path')
    pending = path.with_name(path.name + '.next')
    if pending.exists() or pending.is_symlink():
        raise ValueError('pending release link already exists')
    try:
        pending.symlink_to(target)
        pending.replace(path)
    finally:
        if pending.is_symlink():
            pending.unlink()


def write_json(path, data):
    pending = path.with_name(path.name + '.tmp')
    pending.write_text(json.dumps(data, sort_keys=True) + '\n')
    pending.replace(path)


def failure(root, version):
    path = root / 'failed-releases.json'
    records = json.loads(path.read_text()) if path.exists() else {}
    count = records.get(version, {}).get('attempts', 0) + 1
    records[version] = {'attempts': count, 'retry_at': time.time() + min(21600, 300 * 2 ** min(count, 8))}
    write_json(path, records)


def check_service_ready(target):
    # Type=simple reports active before imports/connect complete.
    active = ['systemctl', '--user', 'is-active', '--quiet', UNITS[0]]
    for _ in range(6):
        time.sleep(5)
        command(active)
    command([target / 'bin/hub2', 'heartbeat'])
    command(active)


def artifact_matches(data, record):
    return len(data) == record['size'] and hashlib.sha256(data).hexdigest() == record['sha256']


def verified_artifact(base, name, record):
    data = fetch(base + name, MAX_ARTIFACT)
    if not artifact_matches(data, record):
        raise ValueError('release checksum mismatch: ' + name)
    return data


def check_host_package(target, manifest):
    command([target / 'bin/python', '-c', 'from agent_hub_host._release import HOST_VERSION, PROTOCOL_VERSION; '
             f'assert HOST_VERSION == {manifest["host_version"]!r} and PROTOCOL_VERSION == {manifest["protocol_version"]}; '
             'import inspect; from agent_hub_host.client import Client; '
             "assert 'native_sessions' in inspect.signature(Client.heartbeat).parameters"])


def enable_service_timer():
    command(['systemctl', '--user', 'enable', UNITS[0]])
    command(['systemctl', '--user', 'enable', '--now', UNITS[2]])


def matches_installed_artifact(path, record):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('refusing to replace an unmanaged path')
    return path.is_file() and path.stat().st_size == record['size'] and artifact_matches(path.read_bytes(), record)


def repair_service_files(base, manifest, root, target):
    # The release is already committed. Do not create a new environment or
    # mark it as an interrupted activation, which would make recovery remove it.
    receipt = target / 'release.json'
    if receipt.is_symlink() or not receipt.is_file():
        raise ValueError('invalid installed release receipt')
    with receipt.open('rb') as stream:
        raw = stream.read(MAX_MANIFEST + 1)
    if len(raw) > MAX_MANIFEST:
        raise ValueError('invalid installed release receipt')
    installed = json.loads(raw)
    validate_manifest(installed)
    if (installed['host_version'] != manifest['host_version']
            or installed['protocol_version'] != manifest['protocol_version']):
        raise ValueError('installed release identity does not match the published manifest')

    installed_artifacts = installed['artifacts']
    artifacts = manifest['artifacts']
    units = Path.home() / '.config/systemd/user'
    paths = {'hub2-self-update.py': root / 'updater.py', **{name: units / name for name in UNITS}}
    # A rebuild can change wheel and zipapp bytes, but these copied source
    # files must have the same content in both manifests.
    for name in paths:
        if any(artifacts[name][field] != installed_artifacts[name][field] for field in ('sha256', 'size')):
            raise ValueError('same-version service artifact mismatch: ' + name)
    for name in ('hub.pyz', 'hub2-enroll.py'):
        if not matches_installed_artifact(target / name, installed_artifacts[name]):
            raise ValueError('installed release artifact mismatch: ' + name)
    check_host_package(target, installed)
    command([target / 'bin/hub2', '--help'])
    replacements = {
        name: verified_artifact(base, name, artifacts[name])
        for name, path in paths.items() if not matches_installed_artifact(path, artifacts[name])
    }
    units.mkdir(parents=True, exist_ok=True)
    for name, data in replacements.items():
        path = paths[name]
        fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            if name == 'hub2-self-update.py':
                Path(temporary).chmod(0o700)
                command([target / 'bin/python', temporary, '--help'])
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    command(['systemctl', '--user', 'daemon-reload'])


def recover(root, releases):
    # .next is our fixed atomic-link scratch name. Remove only links into this
    # release store; never remove a regular file or an unrelated symlink.
    for name in ('current.next', 'previous.next'):
        path = root / name
        if path.is_symlink():
            dest = path.resolve()
            if dest.parent == releases and re.fullmatch(r'[0-9a-f]{40}', dest.name):
                path.unlink()
    for target in releases.iterdir():
        marker = target / '.installing'
        if target.is_symlink() or not re.fullmatch(r'[0-9a-f]{40}', target.name) or not marker.is_file():
            continue
        if marker.read_text() != target.name:
            raise ValueError('unknown interrupted release owner')
        restore_units(target)
        current = root / 'current'
        if current.is_symlink() and current.resolve() == target:
            previous = root / 'previous'
            if previous.is_symlink() and previous.resolve().is_dir() and previous.resolve() != target:
                link(previous.resolve(), current)
                command(['systemctl', '--user', 'restart', 'agent-hub2-host.service'])
            else:
                command(['systemctl', '--user', 'stop', 'agent-hub2-host.service'])
                current.unlink()
        failure(root, target.name)
        shutil.rmtree(target)


def restore_units(target):
    receipt = target / 'units-before.json'
    if not receipt.is_file():
        return
    units = Path.home() / '.config/systemd/user'
    for name, value in json.loads(receipt.read_text()).items():
        if name not in UNITS:
            raise ValueError('unknown unit in rollback receipt')
        if value is None:
            (units / name).unlink(missing_ok=True)
        else:
            (units / name).write_bytes(bytes.fromhex(value))
    command(['systemctl', '--user', 'daemon-reload'])


def prune(root, releases):
    keep = {path.resolve() for path in (root / 'current', root / 'previous') if path.is_symlink()}
    managed = []
    for path in releases.iterdir():
        receipt = path / 'release.json'
        if path.is_symlink() or not re.fullmatch(r'[0-9a-f]{40}', path.name) or not receipt.is_file():
            continue
        if json.loads(receipt.read_text()).get('host_version') == path.name:
            managed.append(path)
    for path in sorted(managed, key=lambda p: p.stat().st_mtime, reverse=True):
        if path in keep:
            continue
        if len(keep) < 3:
            keep.add(path)
        else:
            shutil.rmtree(path)


def install(base_url, root, *, install_service=False, restart=True):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Never delete a lock file: all invocations lock this same inode.
    with (root / 'update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        base = base_url.rstrip('/') + '/cli/'
        manifest = json.loads(fetch(base + 'host-release.json', MAX_MANIFEST))
        host_wheel = validate_manifest(manifest)
        version = manifest['host_version']
        releases = root / 'releases'
        releases.mkdir(exist_ok=True)
        recover(root, releases)
        failures = root / 'failed-releases.json'
        record = json.loads(failures.read_text()).get(version, {}) if failures.exists() else {}
        if record.get('retry_at', 0) > time.time():
            print('Hub2 release retry deferred after failed activation: ' + version)
            if install_service:
                raise RuntimeError('service setup requires a release outside activation backoff')
            return
        current = root / 'current'
        old = current.resolve(strict=True) if current.is_symlink() else None
        if current.exists() and old is None:
            raise ValueError('current release is unmanaged')
        target = releases / version
        if old == target and (target / 'release.json').is_file():
            if install_service:
                repair_service_files(base, manifest, root, target)
                if restart:
                    command(['systemctl', '--user', 'start', UNITS[0]])
                    check_service_ready(target)
                    enable_service_timer()
            print('Hub2 release already current: ' + version)
            return
        if target.exists():
            # Do not reuse a failed installation or overwrite a retained release.
            raise ValueError('release directory exists; explicit recovery required')
        scratch = root / 'tmp'
        scratch.mkdir(exist_ok=True)
        activated = False
        with tempfile.TemporaryDirectory(prefix='download-', dir=scratch) as temporary:
            download = Path(temporary)
            for name, record in manifest['artifacts'].items():
                (download / name).write_bytes(verified_artifact(base, name, record))
            # Rename an empty marked directory BEFORE creating the venv. Venv
            # scripts embed absolute paths, so renaming a built venv is unsafe.
            prepared = download / 'environment'
            prepared.mkdir(mode=0o700)
            (prepared / '.installing').write_text(version)
            prepared.rename(target)
            try:
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
                (target / 'hub.pyz').chmod(0o755)
                command([python, target / 'hub.pyz', '--help'])
                command([python, '-m', 'py_compile', download / 'hub2-self-update.py'])
                command([python, download / 'hub2-self-update.py', '--help'])
                (target / 'release.json').write_text(json.dumps(manifest, sort_keys=True) + '\n')
                if old:
                    link(old, root / 'previous')
                link(target, current)
                activated = True
                units = Path.home() / '.config/systemd/user'
                if install_service or (units / UNITS[0]).exists():
                    units.mkdir(parents=True, exist_ok=True)
                    write_json(target / 'units-before.json', {name: (units / name).read_bytes().hex()
                               if (units / name).exists() else None for name in UNITS})
                    for name in UNITS:
                        shutil.copyfile(download / name, units / name)
                    command(['systemctl', '--user', 'daemon-reload'])
                if restart:
                    command(['systemctl', '--user', 'restart', 'agent-hub2-host.service'])
                    check_service_ready(target)
                # Replace the updater only after successful activation.
                updater = root / 'updater.py.next'
                shutil.copyfile(download / 'hub2-self-update.py', updater)
                updater.chmod(0o700)
                updater.replace(root / 'updater.py')
                (target / '.installing').unlink()
            except BaseException:
                failure(root, version)
                restore_units(target)
                if activated:
                    if old:
                        link(old, current)
                        if restart:
                            command(['systemctl', '--user', 'restart', 'agent-hub2-host.service'])
                    else:
                        if restart:
                            with contextlib.suppress(Exception):
                                command(['systemctl', '--user', 'stop', 'agent-hub2-host.service'])
                        current.unlink()
                raise
            finally:
                # Remove only the new failed environment created by this call.
                if not current.is_symlink() or current.resolve() != target:
                    shutil.rmtree(target)
        # Release activation and updater publication are committed. If enabling
        # the timer fails, leave that working pair in place for same-version retry.
        if install_service and restart:
            enable_service_timer()
        prune(root, releases)
        print('Activated Hub2 release: ' + version)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default=os.environ.get('HUB_BASE_URL'))
    parser.add_argument('--root', type=Path, default=Path.home() / '.local/share/agent-hub2')
    parser.add_argument('--install-service', action='store_true', help='Install user units; enable after readiness when restarting')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--no-restart', action='store_true', help='Stage initial installation before enrollment')
    mode.add_argument('--check-service-ready', action='store_true', help='Observe the started service and verify its enrollment')
    args = parser.parse_args()
    if args.check_service_ready and args.install_service:
        parser.error('--check-service-ready does not install a service')
    try:
        if args.check_service_ready:
            check_service_ready(args.root / 'current')
            return 0
        config = Path.home() / '.config/agent-hub/v2.json'
        base = args.base_url or (json.loads(config.read_text()).get('server') if config.exists() else None)
        install(base or 'https://hub.dtcdev.click', args.root.resolve(),
                install_service=args.install_service, restart=not args.no_restart)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        print('Hub2 release update failed; inspect the current release and retry. No credentials logged.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
