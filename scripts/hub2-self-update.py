#!/usr/bin/env python3
"""Install verified Hub2 releases without modifying a running environment.

The manifest is trusted through HTTPS, not a signature. A SHA check protects
against partial/mixed downloads; it does not replace transport authentication.
"""
import argparse
import contextlib
from contextvars import ContextVar
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
import tomllib
from urllib.parse import urlsplit
from urllib.request import urlopen

UNITS = ('agent-hub2-host.service', 'agent-hub2-host-update.service', 'agent-hub2-host-update.timer')
MAX_MANIFEST = 128 * 1024
MAX_ARTIFACT = 32 * 1024 * 1024
UPDATE_LOCK_FD = ContextVar("update_lock_fd", default=None)


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
    lock_fd = UPDATE_LOCK_FD.get()
    result = subprocess.run([str(value) for value in args], stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=120,
                            pass_fds=() if lock_fd is None else (lock_fd,))
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
    # Receipts must remain private even when the updater inherits umask 0002.
    durable_write(path, (json.dumps(data, sort_keys=True) + '\n').encode(), 0o600)


def failure(root, version):
    path = root / 'failed-releases.json'
    records = json.loads(path.read_text()) if path.exists() else {}
    count = records.get(version, {}).get('attempts', 0) + 1
    records[version] = {'attempts': count, 'retry_at': time.time() + min(21600, 300 * 2 ** min(count, 8))}
    write_json(path, records)


def read_release_receipt(target):
    receipt = target / 'release.json'
    if receipt.is_symlink() or not receipt.is_file():
        raise ValueError('invalid installed release receipt')
    with receipt.open('rb') as stream:
        raw = stream.read(MAX_MANIFEST + 1)
    if len(raw) > MAX_MANIFEST:
        raise ValueError('invalid installed release receipt')
    installed = json.loads(raw)
    validate_manifest(installed)
    return installed


def check_service_ready(target):
    target = target.resolve(strict=True)
    installed = read_release_receipt(target)
    if installed['host_version'] != target.name:
        raise ValueError('installed release receipt does not match release directory')
    config = Path.home() / '.config/agent-hub/v2.json'
    if not config.is_file():
        raise ValueError('missing enrollment config')
    with config.open('rb') as stream:
        raw = stream.read(MAX_MANIFEST + 1)
    enrollment = json.loads(raw) if len(raw) <= MAX_MANIFEST else None
    machine_id = enrollment.get('machine_id') if isinstance(enrollment, dict) else None
    if not isinstance(machine_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', machine_id):
        raise ValueError('invalid enrolled machine identity')

    # Type=simple reports active before imports/connect complete.
    active = ['systemctl', '--user', 'is-active', '--quiet', UNITS[0]]
    for _ in range(6):
        time.sleep(5)
        command(active)

    # The CLI handles authentication; credentials are never command arguments.
    try:
        result = subprocess.run([str(target / 'bin/hub2'), '--config', str(config), 'request', 'GET', '/api/fleet/routing/self'],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
        status = json.loads(result.stdout) if result.returncode == 0 and len(result.stdout) <= 4096 else None
    except (ValueError, OSError, subprocess.TimeoutExpired):
        raise RuntimeError('host daemon connection not verified') from None
    if (not isinstance(status, dict) or status.get('machine_id') != machine_id
            or status.get('connected') is not True or status.get('version_mismatch') is not False
            or status.get('host_version') != installed['host_version']
            or type(status.get('protocol_version')) is not int
            or status['protocol_version'] != installed['protocol_version']):
        raise RuntimeError('host daemon connection not verified')
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


def install_operator_cli(target, manifest):
    # The wheel owns hub2 (daemon administration); hub is the full operator
    # zipapp. Both packages use agent_hub_host, so importing the operator CLI
    # through the host venv would select the wrong package.
    archive = target / 'hub.pyz'
    if not matches_installed_artifact(archive, manifest['artifacts']['hub.pyz']):
        raise ValueError('installed release artifact mismatch: hub.pyz')
    entry = target / 'bin/hub'
    if entry.is_symlink() and os.readlink(entry) == '../hub.pyz':
        return
    command([target / 'bin/python', archive, '--version'])
    entry.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.operator-cli-', dir=entry.parent) as temporary:
        pending = Path(temporary) / 'hub'
        pending.symlink_to('../hub.pyz')
        os.replace(pending, entry)


def repair_service_files(base, manifest, root, target):
    # The release is already committed. Do not create a new environment or
    # mark it as an interrupted activation, which would make recovery remove it.
    installed = read_release_receipt(target)
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


# Managed Claude release re-pin (see agent_hub_host.managed_claude_repin).
MANAGED_PROFILE = 'hub-hub-claude'
REPIN_JOURNAL = 'managed-claude-repin.json'
REPIN_TIMEOUT = 120
REPIN_LOCK_TIMEOUT = 30
REPIN_BOOT = ('import sys; sys.path.insert(0, sys.argv[1]); '
              'from agent_hub_host.managed_claude_repin import main; raise SystemExit(main(sys.argv[1:]))')
# Selectors and tokens never reach the helper; it binds the current enrollment.
REPIN_ENV_DROP = ('HUB_CONFIG_FILE', 'HUB2_CONFIG', 'HUB_TOKEN', 'HUB2_TOKEN', 'HUB_JOB_TOKEN', 'HUB_URL',
                  'HUB_BASE_URL', 'APLEXER_TAG', 'APLEXER_WORKSPACE', 'APLEXER_SESSION_ID')
MANAGED_BEGIN = '# BEGIN HUB MANAGED ENGINES\n'
MANAGED_END = '# END HUB MANAGED ENGINES\n'
MANAGED_HEADING = '[profiles.' + MANAGED_PROFILE + ']\n'


def managed_profile_path():
    # Same selection as provisioning.profile_path on the supported Linux hosts.
    explicit = os.environ.get('APLEXER_CONFIG')
    if explicit:
        return Path(explicit)
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'aplexer' / 'config.toml'


def managed_claude_configured():
    """Cheap local pre-check; the verified release helper decides strictly."""
    try:
        with managed_profile_path().open('rb') as stream:
            raw = stream.read(262145)
    except FileNotFoundError:
        return False
    return MANAGED_PROFILE.encode() in raw


def read_repin_journal(journal):
    if journal.is_symlink() or not journal.is_file():
        raise ValueError('invalid managed Claude re-pin journal')
    with journal.open('rb') as stream:
        raw = stream.read(65537)
    state = json.loads(raw) if len(raw) <= 65536 else None
    keys = {'schema', 'release', 'previous_release', 'profile_path', 'managed_root', 'previous_command',
            'command', 'generation', 'revision', 'verified'}
    if (not isinstance(state, dict) or set(state) != keys or state['schema'] != 1
            or any(not isinstance(state[k], str) or not re.fullmatch(r'[0-9a-f]{40}', state[k])
                   for k in ('release', 'previous_release'))
            or any(not isinstance(state[k], str) or not Path(state[k]).is_absolute() for k in ('profile_path', 'managed_root'))
            or any(not isinstance(state[k], list) or not state[k] or not all(isinstance(v, str) for v in state[k])
                   for k in ('previous_command', 'command'))
            or type(state['generation']) is not int or state['generation'] < 1
            or not isinstance(state['revision'], str) or not re.fullmatch(r'[a-f0-9-]{36}', state['revision'])
            or type(state['verified']) is not bool):
        raise ValueError('invalid managed Claude re-pin journal')
    return state


@contextlib.contextmanager
def managed_lock(managed_root):
    """The provisioning reconcile lock, bounded; never created or replaced here."""
    fd = os.open(managed_root / 'reconcile.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        deadline = time.monotonic() + REPIN_LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('managed Claude lock timeout') from None
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


def managed_owner_matches(state):
    """Generation/ownership fence: the receipt the re-pin verified is still current."""
    root = Path(state['managed_root'])
    try:
        rows = json.loads((root / 'state.json').read_bytes())
        rows = [row for row in rows if isinstance(row, dict) and row.get('label') == 'hub-claude']
        if len(rows) != 1 or rows[0].get('revision') != state['revision'] or rows[0].get('provider') != 'claude':
            return False
        credential = json.loads((root / ('hub-claude-' + state['revision']) / 'credential-state.json').read_bytes())
    except (OSError, ValueError, TypeError):
        return False
    return isinstance(credential, dict) and type(credential.get('generation')) is int and credential['generation'] == state['generation']


def restore_profile_pin(state):
    """Compare-and-set the one managed command back; never a blanket restore."""
    path = Path(state['profile_path'])
    try:
        text = path.read_bytes().decode()
    except FileNotFoundError:
        return 'superseded'
    if not managed_owner_matches(state):
        return 'superseded'
    published = 'command = ' + json.dumps(state['command'])
    previous = 'command = ' + json.dumps(state['previous_command'])
    if text.count(MANAGED_BEGIN) != 1 or text.count(MANAGED_END) != 1:
        return 'superseded'
    before, rest = text.split(MANAGED_BEGIN)
    marked, after = rest.split(MANAGED_END)
    sections = re.split(r'(?m)(?=^\[)', marked)
    targets = [index for index, section in enumerate(sections) if section.startswith(MANAGED_HEADING)]
    if len(targets) != 1:
        return 'superseded'
    lines = sections[targets[0]].split('\n')
    if lines[1:3] == ['engine = "claude"', previous]:
        return 'restored'
    if lines[1:3] != ['engine = "claude"', published] or any(line.strip() for line in lines[3:]):
        return 'superseded'
    lines[2] = previous
    sections[targets[0]] = '\n'.join(lines)
    updated = before + MANAGED_BEGIN + ''.join(sections) + MANAGED_END + after
    original, proposed = tomllib.loads(text), tomllib.loads(updated)
    if (original['profiles'][MANAGED_PROFILE]['command'] != state['command']
            or proposed['profiles'][MANAGED_PROFILE]['command'] != state['previous_command']):
        return 'superseded'
    proposed['profiles'][MANAGED_PROFILE]['command'] = state['command']
    if proposed != original:
        return 'superseded'
    if path.read_bytes().decode() != text:
        return 'superseded'
    durable_write(path, updated.encode(), 0o600)
    return 'restored'


def restore_managed_claude(root):
    """Undo an unverified or rolled-back re-pin, preserving newer owners."""
    journal = root / REPIN_JOURNAL
    if not (journal.exists() or journal.is_symlink()):
        return None
    state = read_repin_journal(journal)
    with managed_lock(Path(state['managed_root'])):
        outcome = restore_profile_pin(state)
    journal.unlink()
    sync_directory(root)
    return outcome


def mark_repin_verified(root):
    journal = root / REPIN_JOURNAL
    if journal.exists() or journal.is_symlink():
        state = read_repin_journal(journal)
        state['verified'] = True
        durable_write(journal, json.dumps(state, sort_keys=True).encode(), 0o600)
        sync_directory(root)


def commit_repin(root):
    journal = root / REPIN_JOURNAL
    if journal.exists() or journal.is_symlink():
        read_repin_journal(journal)
        journal.unlink()
        sync_directory(root)


def recover_repin(root, releases):
    """Before release recovery: keep only a verified pin of the committed release."""
    journal = root / REPIN_JOURNAL
    if not (journal.exists() or journal.is_symlink()):
        return
    state = read_repin_journal(journal)
    release = release_path(releases, state['release'])
    current = root / 'current'
    committed = (state['verified'] and current.is_symlink() and current.resolve() == release
                 and not (release / '.installing').exists() and not (root / 'retained-switch.json').exists())
    if committed:
        commit_repin(root)
    else:
        restore_managed_claude(root)


UPDATER_ROLLBACK = 'updater-rollback.json'


def release_updater(base, manifest, root):
    """This release's verified updater bytes; reuse the installed copy when exact."""
    record = manifest['artifacts']['hub2-self-update.py']
    if matches_installed_artifact(root / 'updater.py', record):
        return (root / 'updater.py').read_bytes()
    return verified_artifact(base, 'hub2-self-update.py', record)


def advance_updater(root, release, data):
    """Make journal-aware recovery code durably reachable before any profile write.

    An older updater would treat the candidate as an interrupted release and
    remove it while the profile still named it. The previous bytes are kept
    until the re-pin commits or its journal is resolved.
    """
    path, record = root / 'updater.py', root / UPDATER_ROLLBACK
    if record.exists() or record.is_symlink():
        raise ValueError('updater rollback pending; explicit recovery required')
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('refusing to replace an unmanaged updater')
    previous = path.read_bytes() if path.is_file() else None
    if previous == data:
        return
    write_json(record, {'release': release, 'previous': None if previous is None else previous.hex()})
    sync_directory(root)
    durable_write(path, data, 0o700)
    sync_directory(root)


def read_updater_rollback(record):
    if record.is_symlink() or not record.is_file():
        raise ValueError('invalid updater rollback record')
    state = json.loads(record.read_bytes()[:4 * MAX_ARTIFACT])
    if (not isinstance(state, dict) or set(state) != {'release', 'previous'}
            or not isinstance(state['release'], str) or not re.fullmatch(r'[0-9a-f]{40}', state['release'])
            or state['previous'] is not None and (not isinstance(state['previous'], str)
                                                  or not re.fullmatch(r'(?:[0-9a-f]{2})+', state['previous']))):
        raise ValueError('invalid updater rollback record')
    return state


def restore_updater(root):
    """Only after the pin journal is resolved; never strands a published pin."""
    record = root / UPDATER_ROLLBACK
    if not (record.exists() or record.is_symlink()):
        return
    if (root / REPIN_JOURNAL).exists() or (root / REPIN_JOURNAL).is_symlink():
        raise RuntimeError('managed Claude pin restore pending')
    state = read_updater_rollback(record)
    if state['previous'] is None:
        (root / 'updater.py').unlink(missing_ok=True)
    else:
        durable_write(root / 'updater.py', bytes.fromhex(state['previous']), 0o700)
    sync_directory(root)
    record.unlink()
    sync_directory(root)


def commit_updater(root):
    record = root / UPDATER_ROLLBACK
    if record.exists() or record.is_symlink():
        read_updater_rollback(record)
        record.unlink()
        sync_directory(root)


def recover_updater(root, releases):
    record = root / UPDATER_ROLLBACK
    if not (record.exists() or record.is_symlink()):
        return
    release = release_path(releases, read_updater_rollback(record)['release'])
    current = root / 'current'
    if (current.is_symlink() and current.resolve() == release and not (release / '.installing').exists()
            and not (root / 'retained-switch.json').exists()):
        commit_updater(root)
    else:
        restore_updater(root)


def repin_managed_claude(target, base, root, updater):
    """Re-pin an already-configured managed Claude target to this verified release.

    ``updater`` returns this release's verified updater bytes; it is installed
    before the helper can publish anything.
    """
    if not managed_claude_configured():
        return None
    journal = root / REPIN_JOURNAL
    if journal.exists() or journal.is_symlink():
        raise ValueError('managed Claude re-pin journal requires recovery')
    installed = read_release_receipt(target)
    if installed['host_version'] != target.name:
        raise ValueError('installed release receipt does not match release directory')
    archive = target / 'hub.pyz'
    if not matches_installed_artifact(archive, installed['artifacts']['hub.pyz']):
        raise ValueError('installed release artifact mismatch: hub.pyz')
    advance_updater(root, target.name, updater())
    env = {key: value for key, value in os.environ.items()
           if key not in REPIN_ENV_DROP and not key.startswith('PYTHON')}
    lock_fd = UPDATE_LOCK_FD.get()
    try:
        result = subprocess.run([str(target / 'bin/python'), '-I', '-S', '-c', REPIN_BOOT, str(archive), 'repin',
                                 base, str(journal)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=REPIN_TIMEOUT, env=env, cwd=str(root),
                                pass_fds=() if lock_fd is None else (lock_fd,))
        status = json.loads(result.stdout) if result.returncode == 0 and len(result.stdout) <= 4096 else None
    except (ValueError, OSError, subprocess.TimeoutExpired):
        status = None
    pending = journal.exists() or journal.is_symlink()
    if (not isinstance(status, dict) or status.get('status') not in ('absent', 'current', 'repinned')
            or pending != (status['status'] == 'repinned')):
        restore_managed_claude(root)
        raise RuntimeError('managed Claude re-pin not verified')
    if pending:
        mark_repin_verified(root)
    return status['status']


def release_path(releases, name):
    if not isinstance(name, str) or not re.fullmatch(r'[0-9a-f]{40}', name):
        raise ValueError('invalid retained release identity')
    return releases / name


def require_release_directory(path):
    if path.is_symlink() or not path.is_dir():
        raise ValueError('retained release is not a managed directory')
    return path


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_write(path, data, mode):
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


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
    (target / 'hub.pyz').chmod(0o755)
    command([python, target / 'hub.pyz', '--help'])
    install_operator_cli(target, manifest)
    write_json(target / 'release.json', manifest)


def sync_environment(target):
    for directory, _, names in os.walk(target, topdown=False, followlinks=False):
        directory = Path(directory)
        for name in names:
            path = directory / name
            if not path.is_symlink():
                with path.open('rb') as stream:
                    os.fsync(stream.fileno())
        sync_directory(directory)


def replace_units(units, files):
    units.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        path = units / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError('refusing to replace an unmanaged unit')
        if data is None:
            path.unlink(missing_ok=True)
            continue
        durable_write(path, data, 0o600)
    sync_directory(units)
    command(['systemctl', '--user', 'daemon-reload'])


def restore_retained_switch(root, releases, journal):
    if journal.is_symlink() or not journal.is_file():
        raise ValueError('invalid retained release journal')
    state = json.loads(journal.read_text())
    old = require_release_directory(release_path(releases, state['current']))
    target = release_path(releases, state['target'])
    backup = releases / ('.retained-' + target.name)
    previous = state['previous']
    if previous is not None:
        previous = release_path(releases, previous)
    current_link, previous_link = root / 'current', root / 'previous'
    if not current_link.is_symlink() or current_link.resolve() not in (old, target):
        raise ValueError('current release changed during retained activation')
    if (previous_link.exists() or previous_link.is_symlink()) and (
            not previous_link.is_symlink() or previous_link.resolve() not in (old, previous)):
        raise ValueError('previous release changed during retained activation')
    if set(state['units']) != set(UNITS):
        raise ValueError('invalid retained unit snapshot')
    files = {name: None if data is None else bytes.fromhex(data) for name, data in state['units'].items()}
    # Once activation began, stop even if recovery already moved the link.
    # The candidate must not import from the retained environment we restore.
    if state.get('switching') is True:
        command(['systemctl', '--user', 'stop', UNITS[0]])
    if current_link.resolve() != old:
        link(old, current_link)
    sync_directory(root)
    if backup.exists() or backup.is_symlink():
        require_release_directory(backup)
        if target.exists() or target.is_symlink():
            shutil.rmtree(require_release_directory(target))
        backup.rename(target)
    sync_directory(releases)
    if previous is None:
        previous_link.unlink(missing_ok=True)
    else:
        link(require_release_directory(previous), previous_link)
    replace_units(Path.home() / '.config/systemd/user', files)
    command(['systemctl', '--user', 'restart', UNITS[0]])
    failure(root, target.name)
    sync_directory(root)
    journal.unlink()
    sync_directory(root)


def finish_retained_switch(root, releases, journal):
    if journal.is_symlink() or not journal.is_file():
        raise ValueError('invalid committed retained release journal')
    state = json.loads(journal.read_text())
    target = require_release_directory(release_path(releases, state['target']))
    if not (root / 'current').is_symlink() or (root / 'current').resolve() != target:
        raise ValueError('current release changed after retained activation')
    # The committed journal must survive a power loss before we discard the
    # only backup, including when cleanup resumes after the journal rename.
    sync_directory(root)
    backup = releases / ('.retained-' + target.name)
    if backup.exists() or backup.is_symlink():
        shutil.rmtree(require_release_directory(backup))
    sync_directory(releases)
    journal.unlink()
    sync_directory(root)


def reactivate_retained_release(base, manifest, host_wheel, root, releases, old, target):
    require_release_directory(target)
    receipt = target / 'release.json'
    if receipt.is_symlink() or not receipt.is_file():
        raise ValueError('release directory exists; explicit recovery required')
    if (target / '.installing').exists() or read_release_receipt(target) != manifest:
        raise ValueError('retained release does not match the published manifest')
    if old is None or require_release_directory(release_path(releases, old.name)) != old:
        raise ValueError('current release is unmanaged')
    previous_link = root / 'previous'
    previous = None
    if previous_link.exists() or previous_link.is_symlink():
        if not previous_link.is_symlink():
            raise ValueError('previous release is unmanaged')
        previous = previous_link.resolve(strict=True)
        if require_release_directory(release_path(releases, previous.name)) != previous:
            raise ValueError('previous release is unmanaged')

    backup = releases / ('.retained-' + target.name)
    if backup.exists() or backup.is_symlink():
        raise ValueError('retained backup already exists; explicit recovery required')
    units = Path.home() / '.config/systemd/user'
    replacements = {}
    snapshot = {}
    for name in UNITS:
        path = units / name
        record = manifest['artifacts'][name]
        matches = matches_installed_artifact(path, record)
        snapshot[name] = path.read_bytes().hex() if path.is_file() else None
        if not matches:
            replacements[name] = verified_artifact(base, name, record)

    scratch = root / 'tmp'
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='download-', dir=scratch) as temporary:
        download = Path(temporary)
        for name in (host_wheel, 'websockets-16.0-py3-none-any.whl', 'hub.pyz', 'hub2-enroll.py'):
            (download / name).write_bytes(verified_artifact(base, name, manifest['artifacts'][name]))
        journal = root / 'retained-switch.json'
        state = {'current': old.name, 'target': target.name,
                 'previous': previous.name if previous else None, 'units': snapshot}
        durable_write(journal, json.dumps(state, sort_keys=True).encode(), 0o600)
        sync_directory(root)
        try:
            # Venv entry points embed this absolute directory. Build at its
            # final path without running any executable from the retained copy.
            target.rename(backup)
            sync_directory(releases)
            target.mkdir(mode=0o700)
            prepare_environment(target, download, manifest, host_wheel)
            sync_environment(target)
            sync_directory(releases)
            if replacements:
                replace_units(units, replacements)
            state['switching'] = True
            durable_write(journal, json.dumps(state, sort_keys=True).encode(), 0o600)
            sync_directory(root)
            link(target, root / 'current')
            command(['systemctl', '--user', 'restart', UNITS[0]])
            check_service_ready(target)
            if json.loads(fetch(base + 'host-release.json', MAX_MANIFEST)) != manifest:
                raise ValueError('published host release changed during retained activation')
            # Same canonical generation, verified readiness and recorded ACK.
            repin_managed_claude(target, base.removesuffix('/cli/'), root,
                                 lambda: release_updater(base, manifest, root))
            link(old, previous_link)
            sync_directory(root)
        except BaseException:
            # Restore the pin before the release links and directories move back.
            # If it cannot be restored, keep the candidate, its backup and both
            # journals for the next recovery rather than strand the pin.
            try:
                restore_managed_claude(root)
            except Exception:
                raise RuntimeError('managed Claude pin restore pending') from None
            restore_updater(root)
            restore_retained_switch(root, releases, journal)
            raise
        # Keep the updater that understands this journal available after rollback.
        committed = root / 'retained-switch-committed.json'
        journal.rename(committed)
        commit_repin(root)
        commit_updater(root)
        finish_retained_switch(root, releases, committed)


def recover(root, releases):
    # .next is our fixed atomic-link scratch name. Remove only links into this
    # release store; never remove a regular file or an unrelated symlink.
    for name in ('current.next', 'previous.next'):
        path = root / name
        if path.is_symlink():
            dest = path.resolve()
            if dest.parent == releases and re.fullmatch(r'[0-9a-f]{40}', dest.name):
                path.unlink()
    recover_repin(root, releases)
    journal = root / 'retained-switch.json'
    if journal.exists() or journal.is_symlink():
        restore_retained_switch(root, releases, journal)
    committed = root / 'retained-switch-committed.json'
    if committed.exists() or committed.is_symlink():
        finish_retained_switch(root, releases, committed)
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
    recover_updater(root, releases)


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


@contextlib.contextmanager
def update_lock(root):
    # Never delete the shared inode. Child installers inherit this descriptor
    # so a killed updater cannot race their writes during the next recovery.
    with (root / 'update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        token = UPDATE_LOCK_FD.set(lock.fileno())
        try:
            yield
        finally:
            UPDATE_LOCK_FD.reset(token)


# Keep this list aligned with native_launch.PROVIDER_BINARIES. node/npm support
# provider entry points and provider-home preparation; the native bridge uses a
# by default, with aplexer also supported as an explicit executable override.
PROVIDER_PATH_COMMANDS = ('codex', 'claude', 'opencode', 'zcodex', 'agy', 'a', 'aplexer', 'node', 'npm')


def preserve_provider_path():
    """Return whether service PATH was newly published, importing no other env.

    This is configuration, not a release artifact: retain it through rollback and
    normal upgrades. Existing files (including explicit empty files or symlinks)
    always belong to the operator and are never rewritten.
    """
    directory = Path.home() / '.config/agent-hub'
    path = directory / 'host.env'
    if path.exists() or path.is_symlink():
        return False
    ambient = os.environ.get('PATH', os.defpath)
    found = set()
    for name in PROVIDER_PATH_COMMANDS:
        executable = shutil.which(name, path=ambient)
        if executable and Path(executable).is_absolute():
            # Do not resolve the executable symlink: npm entry points live in
            # the versioned bin directory alongside their interpreter.
            found.add(str(Path(executable).parent))
    directories = []
    for entry in ambient.split(os.pathsep):
        if not entry or not Path(entry).is_absolute():
            continue
        entry = str(Path(entry))
        if entry in found and entry not in directories:
            directories.append(entry)
    for entry in (str(Path.home() / '.local/bin'), '/usr/local/bin', '/usr/bin', '/bin'):
        if entry not in directories:
            directories.append(entry)
    value = os.pathsep.join(directories)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('provider executable directory contains unsupported characters')
    value = value.replace('\\', '\\\\').replace('"', '\\"')
    data = ('# Provider executable directories discovered during service installation.\n'
            'PATH="' + value + '"\n').encode()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='host.env.', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Publish complete bytes without racing an operator-created file.
            os.link(temporary, path)
        except FileExistsError:
            return False
        sync_directory(directory)
        return True
    finally:
        Path(temporary).unlink(missing_ok=True)


def install(base_url, root, *, install_service=False, restart=True):
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with update_lock(root):
        base = base_url.rstrip('/') + '/cli/'
        releases = root / 'releases'
        releases.mkdir(exist_ok=True)
        recover(root, releases)
        manifest = json.loads(fetch(base + 'host-release.json', MAX_MANIFEST))
        host_wheel = validate_manifest(manifest)
        version = manifest['host_version']
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
        if install_service:
            preserve_provider_path()
        if old == target and (target / 'release.json').is_file():
            installed = read_release_receipt(target)
            if (installed['host_version'] != version
                    or installed['protocol_version'] != manifest['protocol_version']):
                raise ValueError('installed release identity does not match the published manifest')
            # Older updaters install the wheel's hub shim even for a release
            # containing this fix. Repair it on the next same-version check,
            # using the original receipt if a rebuild changed zipapp bytes.
            install_operator_cli(target, installed)
            if install_service:
                repair_service_files(base, manifest, root, target)
                if restart:
                    # Explicit setup must load staged/repaired host.env even if
                    # this same release is already running with the old PATH.
                    command(['systemctl', '--user', 'restart', UNITS[0]])
                    check_service_ready(target)
                    enable_service_timer()
            # Heal a pin left on an older release; nothing else to roll back.
            repin_managed_claude(target, base_url.rstrip('/'), root,
                                 lambda: release_updater(base, manifest, root))
            commit_repin(root)
            commit_updater(root)
            print('Hub2 release already current: ' + version)
            return
        if target.exists() or target.is_symlink():
            if not restart:
                raise ValueError('retained release activation requires a verified restart')
            reactivate_retained_release(base, manifest, host_wheel, root, releases, old, target)
            if install_service:
                enable_service_timer()
            prune(root, releases)
            print('Reactivated retained Hub2 release: ' + version)
            return
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
                prepare_environment(target, download, manifest, host_wheel)
                python = target / 'bin/python'
                command([python, '-m', 'py_compile', download / 'hub2-self-update.py'])
                command([python, download / 'hub2-self-update.py', '--help'])
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
                repin_managed_claude(target, base_url.rstrip('/'), root,
                                     lambda: (download / 'hub2-self-update.py').read_bytes())
                # Replace the updater only after successful activation.
                updater = root / 'updater.py.next'
                shutil.copyfile(download / 'hub2-self-update.py', updater)
                updater.chmod(0o700)
                updater.replace(root / 'updater.py')
                (target / '.installing').unlink()
            except BaseException:
                # A failed restore keeps the candidate, its marker, the journal
                # and the advanced updater for the next recovery.
                try:
                    restore_managed_claude(root)
                    pin_restored = True
                except Exception:
                    pin_restored = False
                if pin_restored:
                    restore_updater(root)
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
                if not pin_restored:
                    raise RuntimeError('managed Claude pin restore pending') from None
                raise
            finally:
                # Remove only the new failed environment created by this call,
                # and never while a published pin may still name it.
                if ((not current.is_symlink() or current.resolve() != target)
                        and not (root / REPIN_JOURNAL).exists()):
                    shutil.rmtree(target)
        # Release activation and updater publication are committed. If enabling
        # the timer fails, leave that working pair in place for same-version retry.
        commit_repin(root)
        commit_updater(root)
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
    mode.add_argument('--check-service-ready', action='store_true', help='Verify the started service is connected with its installed release')
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
