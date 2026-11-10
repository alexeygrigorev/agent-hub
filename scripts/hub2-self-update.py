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
    if manifest.get('schema_version') != 1 or type(manifest.get('protocol_version')) is not int or manifest['protocol_version'] < 1:
        raise ValueError('unsupported release protocol')
    requirement = re.fullmatch(r'>=(\d+)\.(\d+)', str(manifest.get('python_requires', '')))
    if not requirement or sys.version_info[:2] < tuple(map(int, requirement.groups())):
        raise ValueError('release requires a newer or unsupported Python runtime')
    if not re.fullmatch(r'[0-9a-f]{40}', str(manifest.get('host_version', ''))):
        raise ValueError('invalid host version')
    files = manifest.get('artifacts')
    if not isinstance(files, dict) or not 6 <= len(files) <= 20:
        raise ValueError('invalid release artifact list')
    for name, record in files.items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]*', name) or name in ('.', '..'):
            raise ValueError('unsafe artifact name')
        if not isinstance(record, dict) or not re.fullmatch(r'[0-9a-f]{64}', str(record.get('sha256', ''))):
            raise ValueError('invalid artifact checksum')
        if type(record.get('size')) is not int or not 0 < record['size'] <= MAX_ARTIFACT:
            raise ValueError('invalid artifact size')
    required = {'hub.pyz', 'hub2-self-update.py', 'hub2-enroll.py', 'host-install.sh', *UNITS}
    if not required <= files.keys():
        raise ValueError('release is incomplete')
    host = [name for name in files if re.fullmatch(r'agent_hub_host_v2-2\.0\.0\+g[0-9a-f]{40}-py3-none-any\.whl', name)]
    if len(host) != 1 or manifest['host_version'] not in host[0] or 'websockets-16.0-py3-none-any.whl' not in files:
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


def stable_service():
    # Type=simple reports active before imports/connect complete. Observe the
    # process over a real interval and prove an authenticated host heartbeat.
    for _ in range(6):
        time.sleep(5)
        command(['systemctl', '--user', 'is-active', '--quiet', 'agent-hub2-host.service'])


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
        manifest = json.loads(fetch(base + 'host-release.json', 128 * 1024))
        host_wheel = validate_manifest(manifest)
        version = manifest['host_version']
        releases = root / 'releases'
        releases.mkdir(exist_ok=True)
        recover(root, releases)
        failures = root / 'failed-releases.json'
        record = json.loads(failures.read_text()).get(version, {}) if failures.exists() else {}
        if record.get('retry_at', 0) > time.time():
            print('Hub2 release retry deferred after failed activation: ' + version)
            return
        current = root / 'current'
        old = current.resolve(strict=True) if current.is_symlink() else None
        if current.exists() and old is None:
            raise ValueError('current release is unmanaged')
        target = releases / version
        if old == target and (target / 'release.json').is_file():
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
                data = fetch(base + name, MAX_ARTIFACT)
                if len(data) != record['size'] or hashlib.sha256(data).hexdigest() != record['sha256']:
                    raise ValueError('release checksum mismatch: ' + name)
                (download / name).write_bytes(data)
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
                command([python, '-c', 'from agent_hub_host._release import HOST_VERSION, PROTOCOL_VERSION; '
                         f'assert HOST_VERSION == {version!r} and PROTOCOL_VERSION == {manifest["protocol_version"]}; '
                         'import inspect; from agent_hub_host.client import Client; '
                         "assert 'native_sessions' in inspect.signature(Client.heartbeat).parameters"])
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
                if install_service:
                    command(['systemctl', '--user', 'enable', 'agent-hub2-host.service', 'agent-hub2-host-update.timer'])
                if restart:
                    command(['systemctl', '--user', 'restart', 'agent-hub2-host.service'])
                    stable_service()
                    command([target / 'bin/hub2', 'heartbeat'])
                # Replace the updater only after successful activation.
                updater = root / 'updater.py.next'
                shutil.copyfile(download / 'hub2-self-update.py', updater)
                updater.chmod(0o700)
                updater.replace(root / 'updater.py')
                if install_service and restart:
                    command(['systemctl', '--user', 'start', 'agent-hub2-host-update.timer'])
                (target / '.installing').unlink()
                prune(root, releases)
                print('Activated Hub2 release: ' + version)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default=os.environ.get('HUB_BASE_URL'))
    parser.add_argument('--root', type=Path, default=Path.home() / '.local/share/agent-hub2')
    parser.add_argument('--install-service', action='store_true', help='Install/enable user units after enrollment')
    parser.add_argument('--no-restart', action='store_true', help='Stage initial installation before enrollment')
    args = parser.parse_args()
    try:
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
