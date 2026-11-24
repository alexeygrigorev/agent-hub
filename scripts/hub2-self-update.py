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
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from urllib.parse import urlsplit
from urllib.request import urlopen
import zipfile

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


def host_cli(target):
    # The host daemon CLI is `hub host`; the release venv runs it directly.
    return [target / 'bin/python', '-I', '-m', 'agent_hub_host.cli']


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
    verify_alert_client(target, installed)
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
        result = subprocess.run([*map(str, host_cli(target)), '--config', str(config), 'request', 'GET', '/api/fleet/routing/self'],
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
    check = '''
import importlib
import importlib.util
import inspect
from pathlib import Path
import sys

expected_version, expected_protocol, candidate = sys.argv[1:]
candidate = Path(candidate).resolve(strict=True)

def is_candidate_path(value):
    try:
        Path(value).resolve(strict=True).relative_to(candidate)
        return True
    except (OSError, ValueError):
        return False

namespaces = ('agent_hub_host', 'hub2_agent_hub_host')
present = []
for namespace in namespaces:
    spec = importlib.util.find_spec(namespace)
    if spec is None:
        continue
    locations = spec.submodule_search_locations
    if locations is None or not locations or not all(is_candidate_path(path) for path in locations):
        raise SystemExit('host namespace is outside the candidate environment')
    present.append(namespace)
if len(present) != 1:
    raise SystemExit('candidate must contain exactly one supported host namespace')

namespace = present[0]
release = importlib.import_module(namespace + '._release')
client = importlib.import_module(namespace + '.client')
if not is_candidate_path(release.__file__) or not is_candidate_path(client.__file__):
    raise SystemExit('host modules are outside the candidate environment')
if type(release.HOST_VERSION) is not str or release.HOST_VERSION != expected_version:
    raise SystemExit('candidate host version mismatch')
if (type(release.PROTOCOL_VERSION) is not int
        or release.PROTOCOL_VERSION != int(expected_protocol)):
    raise SystemExit('candidate protocol mismatch')
if 'native_sessions' not in inspect.signature(client.Client.heartbeat).parameters:
    raise SystemExit('candidate heartbeat contract is incomplete')
'''
    command([target / 'bin/python', '-I', '-c', check, manifest['host_version'],
             str(manifest['protocol_version']), str(target.resolve())])


def enable_service_timer():
    command(['systemctl', '--user', 'enable', UNITS[0]])
    command(['systemctl', '--user', 'enable', '--now', UNITS[2]])


def matches_installed_artifact(path, record):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('refusing to replace an unmanaged path')
    return path.is_file() and path.stat().st_size == record['size'] and artifact_matches(path.read_bytes(), record)



ALERT_CLIENT = 'hub_alert_client.py'


def verify_alert_client(target, manifest):
    record = manifest['artifacts'].get(ALERT_CLIENT)
    if record is not None and not matches_installed_artifact(target / ALERT_CLIENT, record):
        raise ValueError('installed release artifact mismatch: ' + ALERT_CLIENT)


def retain_alert_client(target, manifest, data):
    record = manifest['artifacts'][ALERT_CLIENT]
    if not artifact_matches(data, record):
        raise ValueError('artifact checksum mismatch: ' + ALERT_CLIENT)
    # Never replace a foreign symlink or special file, including during repair.
    matches_installed_artifact(target / ALERT_CLIENT, record)
    durable_write(target / ALERT_CLIENT, data, 0o600)
    verify_alert_client(target, manifest)


def repair_alert_client(base, manifest, installed, target):
    record = installed['artifacts'].get(ALERT_CLIENT)
    if manifest['artifacts'].get(ALERT_CLIENT) != record:
        raise ValueError('same-version companion artifact mismatch: ' + ALERT_CLIENT)
    if record is not None and not matches_installed_artifact(target / ALERT_CLIENT, record):
        retain_alert_client(target, installed, verified_artifact(base, ALERT_CLIENT, record))
    verify_alert_client(target, installed)


def _hub2_entry_declared(entry_points):
    """True when a console_scripts section declares the private legacy `hub2` entry."""
    section = None
    for line in entry_points.splitlines():
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            section = stripped[1:-1].strip().lower().replace('_', ' ')
            continue
        if section == 'console scripts' and stripped.split('=', 1)[0].strip() == 'hub2':
            return True
    return False


def _wheel_hub2_declared(wheel):
    if wheel is None:
        return None
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = [name for name in archive.namelist()
                     if name.endswith('.dist-info/entry_points.txt')]
            declared = any(_hub2_entry_declared(archive.read(name).decode('utf-8', 'replace'))
                           for name in names)
    except (OSError, zipfile.BadZipFile, RuntimeError):
        # Unreadable/placeholder wheel bytes: fall back to the installed
        # environment instead of guessing the entry contract.
        return None
    return declared


_ENTRY_POINTS_PATTERNS = ('lib/python3.*/site-packages/*.dist-info/entry_points.txt',
                          'lib/site-packages/*.dist-info/entry_points.txt',
                          'Lib/site-packages/*.dist-info/entry_points.txt')


def _installed_hub2_declared(target):
    for pattern in _ENTRY_POINTS_PATTERNS:
        for path in sorted(target.glob(pattern)):
            try:
                text = path.read_text(errors='replace')
            except OSError:
                continue
            if _hub2_entry_declared(text):
                return True
    return False


# The pinned HOST distribution and the two supported console-entry targets:
# the private legacy daemon entry and the unified operator entry that
# replaced it. Anything else proves nothing about a release's entry contract.
_HOST_DIST_INFO_PREFIX = 'agent_hub_host_v2-'
_LEGACY_HUB2_ENTRY_TARGET = 'agent_hub_host.cli:main'
_UNIFIED_HUB_ENTRY_TARGET = 'agent_hub_host.wheel_cli:main'


def _console_scripts_entries(entry_points):
    """Parsed console_scripts entries of an entry_points.txt text.

    Returns the declared name -> target mapping, or None when no
    console_scripts section exists, a line inside one is malformed, a name
    is declared twice, or the console_scripts section itself repeats:
    duplicated declarations are ambiguous metadata and never resolve by
    last-wins."""
    section = None
    seen_console_section = False
    seen_entry = False
    entries = {}
    for line in entry_points.splitlines():
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            section = stripped[1:-1].strip().lower().replace('_', ' ')
            if section == 'console scripts':
                if seen_console_section:
                    return None  # repeated section: ambiguous merge, never last-wins
                seen_console_section = True
            continue
        if section != 'console scripts' or not stripped:
            continue
        name_part, separator, target_part = stripped.partition('=')
        if not separator or not name_part.strip() or not target_part.strip():
            return None
        name = name_part.strip()
        if name in entries:
            return None  # duplicate declaration: ambiguous, never last-wins
        seen_entry = True
        entries[name] = target_part.strip()
    return entries if seen_entry else None


def _entry_declaration_of_entries(entries):
    """Legacy (True) / unified (False) declaration of one entry_points file.

    Legacy requires the `hub2` entry with the supported legacy target; the
    unified contract requires `hub` with the `wheel_cli` target and `hub2`
    absent — a merely readable or unrelated file declares nothing. None is
    the unknown contract: empty or foreign metadata, a hub2 entry with an
    unsupported target, or a contradictory mix of both contracts."""
    declared = None
    if entries.get('hub2') == _LEGACY_HUB2_ENTRY_TARGET:
        declared = True
    elif 'hub2' in entries:
        return None
    if entries.get('hub') == _UNIFIED_HUB_ENTRY_TARGET:
        if declared:
            return None
        declared = False
    return declared


def _release_entry_declaration(release_directory):
    """Three-state console-entry declaration of a release's own HOST
    dist-info metadata.

    True when the release's own `agent_hub_host_v2-*` dist-info positively
    declares the private legacy `hub2` console entry with the supported
    `agent_hub_host.cli:main` target, False when it positively declares the
    unified `hub` entry with the `agent_hub_host.wheel_cli:main` target and
    no `hub2` entry, and None whenever that exact contract is not proven:
    no host dist-info, an unreadable, empty, unrelated, malformed or
    target-less entry_points file, or two host dist-infos with conflicting
    declarations. An unknown contract is never a capability."""
    declaration = None
    for pattern in _ENTRY_POINTS_PATTERNS:
        for path in sorted(release_directory.glob(pattern)):
            if not path.parent.name.startswith(_HOST_DIST_INFO_PREFIX):
                continue  # an unrelated distribution never proves this release's contract
            try:
                text = path.read_text(errors='replace')
            except OSError:
                return None
            declared = _entry_declaration_of_entries(_console_scripts_entries(text) or {})
            if declared is None:
                return None
            if declaration is not None and declaration != declared:
                return None  # two host dist-infos disagree: ambiguous
            declaration = declared
    return declaration


def _recognized_legacy_hub2_launcher(launcher, python):
    """Read-only recognition of the managed legacy daemon launcher.

    The same contract _ensure_legacy_hub2_launcher keeps and rebuilds: a
    regular file whose shebang names the lexical release venv interpreter
    and whose body imports the host CLI main. An empty or foreign
    placeholder is not a legacy capability.
    """
    if launcher.is_symlink() or not launcher.is_file():
        return False
    if os.name == 'nt':
        return launcher.suffix == '.exe'
    try:
        body = launcher.read_text(errors='replace')
    except OSError:
        return False
    shebang = body.splitlines(True)[:1]
    expected = '#!' + os.path.abspath(python)
    return bool(shebang) and shebang[0].rstrip('\n') == expected \
        and 'agent_hub_host.cli' in body and 'main' in body


def _ensure_legacy_hub2_launcher(target):
    """Keep or rebuild the private legacy daemon launcher the target requires.

    Releases published before CLI unification own a `bin/hub2` daemon console
    entry and their service unit names it. The public operator zipapp of those
    releases has no `host` command, so deleting the launcher would strand the
    release. A symlink or unrecognized file is refused, never replaced.
    """
    launcher = target / 'bin/hub2'
    python = target / 'bin/python'
    if launcher.exists() or launcher.is_symlink():
        if launcher.is_symlink() or not launcher.is_file():
            raise ValueError('refusing to keep an unmanaged legacy hub2 launcher')
        if not _recognized_legacy_hub2_launcher(launcher, python):
            # The pip console launcher names the lexical release venv
            # interpreter; bin/python is typically a symlink to the base
            # interpreter, and resolving it would select the base
            # prefix/site-packages where the retained wheel is not installed.
            raise ValueError('unrecognized legacy hub2 launcher')
        return
    if os.name == 'nt':
        raise ValueError('legacy hub2 launcher missing from the release environment')
    launcher.parent.mkdir(exist_ok=True)
    launcher.write_text(
        '#!' + os.path.abspath(python) + '\n'
        'import sys\n'
        'from agent_hub_host.cli import main\n'
        "if __name__ == '__main__':\n"
        '    sys.exit(main())\n')
    launcher.chmod(0o755)


def install_operator_cli(target, manifest, wheel=None):
    # The wheel owns the daemon module run by `hub host`; hub is the full operator
    # zipapp. Both packages use agent_hub_host, so importing the operator CLI
    # through the host venv would select the wrong package.
    # The daemon entry contract follows the verified target release, not this
    # updater: releases whose wheel still declares the private `hub2` console
    # entry keep that launcher (their unit ExecStart names it and their zipapp
    # has no `host` command); unified releases have no separate hub2 CLI.
    archive = target / 'hub.pyz'
    if not matches_installed_artifact(archive, manifest['artifacts']['hub.pyz']):
        raise ValueError('installed release artifact mismatch: hub.pyz')
    declared = _wheel_hub2_declared(wheel)
    if declared is None:
        declared = _installed_hub2_declared(target)
    entry = target / 'bin/hub'
    if declared:
        _ensure_legacy_hub2_launcher(target)
    else:
        (target / 'bin/hub2').unlink(missing_ok=True)  # there is no separate hub2 CLI
    if entry.is_symlink() and os.readlink(entry) == '../hub.pyz':
        return
    command([target / 'bin/python', archive, '--version'])
    entry.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.operator-cli-', dir=entry.parent) as temporary:
        pending = Path(temporary) / 'hub'
        pending.symlink_to('../hub.pyz')
        os.replace(pending, entry)


REGULAR_CLI_BOOT = "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('agent_hub_host.regular_cli',run_name='__main__')"


def update_regular_operator_cli(root, target, manifest):
    # No record means no existing regular-path ownership: zero target lookup or
    # background adoption. Component failure never poisons a healthy Host update.
    try:
        os.lstat(root / 'regular-cli')
    except FileNotFoundError:
        return 'unmanaged'
    except OSError:
        print('Regular operator CLI component: unknown')
        return 'unknown'
    try:
        if not matches_installed_artifact(target / 'hub.pyz', manifest['artifacts']['hub.pyz']):
            raise ValueError('regular CLI archive mismatch')
        args = [str(target / 'bin/python'), '-I', '-S', '-c', REGULAR_CLI_BOOT,
                str(target / 'hub.pyz'), '--root', str(root), '--archive', str(target / 'hub.pyz'),
                '--manifest', str(target / 'release.json')]
        inherited = UPDATE_LOCK_FD.get()
        if inherited is not None:
            args += ['--lock-fd', str(inherited)]
        result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=22, pass_fds=() if inherited is None else (inherited,))
        state = 'updated' if result.returncode == 0 else 'deferred'
    except Exception:
        state = 'unknown'
    print('Regular operator CLI component: ' + state)
    return state


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
    command([*host_cli(target), '--help'])
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
            or type(state['verified']) is not bool
            or Path(state['command'][0]).parent.parent.name != state['release']
            or Path(state['previous_command'][0]).parent.parent.name != state['previous_release']):
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


def persist_profile(path):
    """Make the observed profile bytes and directory entry durable, or its absence.

    When the profile (or whole directories above it) is gone, the removal is
    durable only once the nearest existing ancestor is synced; that barrier is
    never skipped and its failure propagates so the journal is kept.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        pass
    else:
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    directory = Path(path).parent
    while not directory.is_dir():
        if directory.parent == directory:
            raise RuntimeError('managed Claude pin restore pending')
        directory = directory.parent
    sync_directory(directory)


def profile_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from profile_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from profile_strings(item)


def profile_unreferenced(text, candidate):
    """Positive proof: neither raw bytes nor any parsed string names the candidate."""
    needle = str(candidate)
    if needle in text:
        return False
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return False
    return not any(needle in value for value in profile_strings(document))


def compare_and_set_pin(path, text, state):
    """Put the one managed command back when it is exactly ours; else None."""
    published = 'command = ' + json.dumps(state['command'])
    previous = 'command = ' + json.dumps(state['previous_command'])
    if text.count(MANAGED_BEGIN) != 1 or text.count(MANAGED_END) != 1:
        return None
    before, rest = text.split(MANAGED_BEGIN)
    marked, after = rest.split(MANAGED_END)
    sections = re.split(r'(?m)(?=^\[)', marked)
    targets = [index for index, section in enumerate(sections) if section.startswith(MANAGED_HEADING)]
    if len(targets) != 1:
        return None
    lines = sections[targets[0]].split('\n')
    if lines[1:3] == ['engine = "claude"', previous]:
        # A previous attempt may have replaced it without a durable directory.
        return 'restored'
    if lines[1:3] != ['engine = "claude"', published] or any(line.strip() for line in lines[3:]):
        return None
    lines[2] = previous
    sections[targets[0]] = '\n'.join(lines)
    updated = before + MANAGED_BEGIN + ''.join(sections) + MANAGED_END + after
    try:
        original, proposed = tomllib.loads(text), tomllib.loads(updated)
    except tomllib.TOMLDecodeError:
        return None
    if (original['profiles'][MANAGED_PROFILE]['command'] != state['command']
            or proposed['profiles'][MANAGED_PROFILE]['command'] != state['previous_command']):
        return None
    proposed['profiles'][MANAGED_PROFILE]['command'] = state['command']
    if proposed != original or path.read_bytes().decode() != text:
        return None
    durable_write(path, updated.encode(), 0o600)
    return 'restored'


def restore_profile_pin(state):
    """Compare-and-set the one managed command back; never a blanket restore.

    ``superseded`` requires positive proof that the profile no longer names the
    candidate release. A newer owner, generation or layout alone is not proof:
    the restore stays pending and the candidate and journal are kept. Every
    outcome is made durable before the caller may retire the journal.
    """
    path = Path(state['profile_path'])
    candidate = Path(state['command'][0]).parent.parent
    try:
        text = path.read_bytes().decode()
    except FileNotFoundError:
        persist_profile(path)
        return 'superseded'
    except UnicodeDecodeError:
        raise RuntimeError('managed Claude pin restore pending') from None
    outcome = compare_and_set_pin(path, text, state) if managed_owner_matches(state) else None
    # Whatever happened to the Claude table, the complete resulting config,
    # including unrelated profiles, must no longer name the candidate.
    try:
        final = path.read_bytes().decode()
    except FileNotFoundError:
        final = None
    except UnicodeDecodeError:
        raise RuntimeError('managed Claude pin restore pending') from None
    if final is not None and not profile_unreferenced(final, candidate):
        raise RuntimeError('managed Claude pin restore pending')
    persist_profile(path)
    return outcome or 'superseded'


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


def activation_barrier(root, releases, name):
    """Persist the committed activation before recovery metadata is retired.

    Recovery would treat a resurrected ``.installing`` marker or missing link as
    an interrupted release and delete the candidate, so the marker removal,
    the release entry, the links and the updater must be durable first.
    """
    target = require_release_directory(release_path(releases, name))
    current = root / 'current'
    if (not current.is_symlink() or current.resolve() != target or (target / '.installing').exists()
            or (root / 'retained-switch.json').exists() or (root / 'retained-switch.json').is_symlink()):
        raise ValueError('managed Claude re-pin activation is not committed')
    sync_directory(target)
    sync_directory(releases)
    sync_directory(root)


def commit_repin(root, releases):
    journal = root / REPIN_JOURNAL
    if journal.exists() or journal.is_symlink():
        state = read_repin_journal(journal)
        if not state['verified']:
            raise ValueError('managed Claude re-pin is not verified')
        activation_barrier(root, releases, state['release'])
        persist_profile(Path(state['profile_path']))
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
    # A missing candidate is never committed, whatever the links say.
    committed = (state['verified'] and not release.is_symlink() and release.is_dir()
                 and current.is_symlink() and current.resolve() == release
                 and not (release / '.installing').exists() and not (root / 'retained-switch.json').exists())
    if committed:
        commit_repin(root, releases)
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


def commit_updater(root, releases):
    record = root / UPDATER_ROLLBACK
    if record.exists() or record.is_symlink():
        activation_barrier(root, releases, read_updater_rollback(record)['release'])
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
        commit_updater(root, releases)
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
    # Fresh payloads and the candidate's entry in the releases parent must be
    # durable before any profile can name them.
    sync_environment(target)
    sync_directory(target.parent)
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


# Other managed engine profiles (hub-antigravity, ...) run the same
# `provisioning.execute()` entry point but were never re-pinned: they kept the
# archive of whichever CLI last ran a sync (a one-off recovery hub.pyz on
# hetzner-aplexer), so session provisioning fixes such as the operator DataOps
# login and dapier session link never reached them (intake fc36c32b).
EXECUTE_CODE = re.compile(
    r"import sys; (?:sys\.path\.insert\(0, '[^'\\]+'\); )+import os; "
    r"os\.environ\[\"AGENT_HUB_MANAGED_DIR\"\] = '(?P<managed>[^'\\]+)'; "
    r"from agent_hub_host\.provisioning import execute; execute\(\)\Z")


CODEX_PROFILE = 'hub-shared-codex'


def release_execute_command(target, label, managed_dir, *, engine=None):
    archive = target / 'hub.pyz'
    if engine == 'codex' and label == CODEX_PROFILE[4:]:
        # Byte-for-byte what provisioning.module_command() generates inside this
        # release; codex_exec_readiness compares the profile to exactly that.
        code = ('import sys; sys.path.insert(0, ' + repr(str(archive)) + '); sys.path.insert(0, ' + repr(str(archive))
                + '); import os; os.environ["AGENT_HUB_MANAGED_DIR"] = ' + repr(str(managed_dir)) + '; '
                'from agent_hub_host.provisioning import execute; execute()')
        return [str(target / 'bin/python'), '-c', code, label]
    code = ('import sys; sys.path.insert(0, ' + repr(str(archive)) + '); import os; '
            'os.environ["AGENT_HUB_MANAGED_DIR"] = ' + repr(str(managed_dir)) + '; '
            'from agent_hub_host.provisioning import execute; execute()')
    return [str(target / 'bin/python'), '-I', '-S', '-c', code, label]


def repinnable_execute_profile(name, table):
    """The managed dir of a generated archive-pinned execute() profile, or None."""
    if (not name.startswith('hub-') or name == MANAGED_PROFILE or not isinstance(table, dict)
            or set(table) != {'engine', 'command'} or not isinstance(table['command'], list)
            or not all(isinstance(part, str) for part in table['command'])):
        return None
    command = table['command']
    if len(command) < 4 or command[-1] != name[4:] or command[-3] != '-c':
        return None
    match = EXECUTE_CODE.fullmatch(command[-2])
    # Only zipapp pins; a checkout/venv source tree is operator-maintained, except
    # the maintained shared-codex profile: a stale source-tree pin there makes the
    # host refuse every codex launch (intake f88ae417), so it follows the release.
    if not match or ('.pyz' not in command[-2]
                     and (name != CODEX_PROFILE or table['engine'] != 'codex')):
        return None
    return match.group('managed')


def repinnable_launcher_script(name, table):
    """(launcher_path, managed_dir) for a generated engine-launch/<label>.sh, or None."""
    if (not name.startswith('hub-') or name == MANAGED_PROFILE or not isinstance(table, dict)
            or set(table) != {'engine', 'command'} or not isinstance(table['command'], list)
            or len(table['command']) != 2 or not all(isinstance(part, str) for part in table['command'])):
        return None
    label = name[4:]
    if table['command'][1] != label:
        return None
    launcher = Path(table['command'][0])
    if launcher.parent.name != 'engine-launch' or launcher.name != label + '.sh':
        return None
    if not launcher.is_file() or launcher.is_symlink():
        return None
    try:
        raw = launcher.read_bytes()[:8193]
    except OSError:
        return None
    if len(raw) > 8192:
        return None
    lines = raw.decode('utf-8', errors='replace').splitlines()
    if len(lines) != 2 or lines[0] != '#!/bin/sh':
        return None
    try:
        parts = shlex.split(lines[1])
    except ValueError:
        return None
    if len(parts) != 5 or parts[0] != 'exec' or parts[2] != '-c' or parts[4] != '$@':
        return None
    match = EXECUTE_CODE.fullmatch(parts[3])
    if not match or '.pyz' not in parts[3]:
        return None
    return launcher, match.group('managed')


def _repin_launcher_script(launcher, target, label, managed, *, engine=None):
    command = release_execute_command(target, label, managed, engine=engine)
    body = '#!/bin/sh\nexec ' + shlex.quote(command[0]) + ' -c ' + shlex.quote(command[-2]) + ' "$@"\n'
    encoded = body.encode('utf-8')
    if launcher.read_bytes() == encoded:
        return False
    temporary = launcher.with_name(launcher.name + '.hub2-repin')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o700)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o700)
        os.replace(temporary, launcher)
    finally:
        temporary.unlink(missing_ok=True)
    sync_directory(launcher.parent)
    return True


def repin_managed_profiles(target):
    """Point every other generated managed execute() profile at this release.

    Best effort and bounded to the marked block: a failure leaves the file as it
    was and never fails the update (the Claude pin has its own fenced path).
    Returns the re-pinned profile names.
    """
    path = managed_profile_path()
    try:
        archive = target / 'hub.pyz'
        if not archive.is_file() or not (target / 'bin/python').is_file():
            return []
        with path.open('rb') as stream:
            raw = stream.read(262145)
        if len(raw) > 262144:
            return []
        text = raw.decode()
        if text.count(MANAGED_BEGIN) != 1 or text.count(MANAGED_END) != 1:
            return []
        before, rest = text.split(MANAGED_BEGIN)
        block, after = rest.split(MANAGED_END)
        profiles = tomllib.loads(block).get('profiles', {})
        changed = []
        script_targets = []
        for name, table in profiles.items():
            managed = repinnable_execute_profile(name, table)
            if managed is None:
                found_script = repinnable_launcher_script(name, table)
                if found_script is not None:
                    script_targets.append((name, table['engine'], found_script[0], found_script[1]))
                continue
            command = release_execute_command(target, name[4:], managed, engine=table['engine'])
            if command == table['command']:
                continue
            old_line = 'command = ' + json.dumps(table['command']) + '\n'
            heading = '[profiles.' + name + ']\n'
            start = block.find(heading)
            index = block.find(old_line, start)
            if start < 0 or index < 0 or block.find('\n[', start + len(heading), index) >= 0:
                continue
            block = block[:index] + 'command = ' + json.dumps(command) + '\n' + block[index + len(old_line):]
            changed.append(name)
        for name, engine, launcher, managed in script_targets:
            managed_root = Path(managed)
            lock = managed_lock(managed_root) if (managed_root / 'reconcile.lock').is_file() else contextlib.nullcontext()
            with lock:
                if _repin_launcher_script(launcher, target, name[4:], managed, engine=engine):
                    changed.append(name)
        config_changed = [name for name in changed if name not in {item[0] for item in script_targets}]
        if not config_changed:
            return changed
        updated = before + MANAGED_BEGIN + block + MANAGED_END + after
        parsed = tomllib.loads(updated).get('profiles', {})
        if any(parsed.get(name, {}).get('command') != release_execute_command(
                target, name[4:], repinnable_execute_profile(name, profiles[name]),
                engine=parsed.get(name, {}).get('engine')) for name in config_changed):
            return []
        managed_root = Path(repinnable_execute_profile(config_changed[0], profiles[config_changed[0]]))
        lock = managed_lock(managed_root) if (managed_root / 'reconcile.lock').is_file() else contextlib.nullcontext()
        with lock:
            with path.open('rb') as stream:
                if stream.read(262145) != raw:
                    return []
            temporary = path.with_name(path.name + '.hub2-repin')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(updated.encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            sync_directory(path.parent)
        return changed
    except (OSError, ValueError, TypeError, KeyError, UnicodeDecodeError, TimeoutError, tomllib.TOMLDecodeError) as exc:
        print('managed profile re-pin skipped: ' + type(exc).__name__, file=sys.stderr)
        return []


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
        # The replacement is durable only once its directory entry is.
        sync_directory(path.parent)
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
    command([*host_cli(target), '--help'])
    shutil.copyfile(download / 'hub.pyz', target / 'hub.pyz')
    shutil.copyfile(download / 'hub2-enroll.py', target / 'hub2-enroll.py')
    if ALERT_CLIENT in manifest['artifacts']:
        retain_alert_client(target, manifest, (download / ALERT_CLIENT).read_bytes())
    (target / 'hub.pyz').chmod(0o755)
    command([python, target / 'hub.pyz', '--help'])
    install_operator_cli(target, manifest, download / host_wheel)
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


def _owned_host_entry_pattern(root):
    """Exact owned ExecStart program-token forms for this root.

    The pattern is applied only at the start of the ExecStart program token
    (see _execstart_program_token); owned forms anywhere else on the line are
    argument data and never match.
    """
    root_norm = os.path.normpath(str(root))
    forms = [re.escape(root_norm.rstrip(os.sep))]
    home = os.path.normpath(str(Path.home()))
    try:
        relative = Path(root_norm).relative_to(home)
        forms.append(re.escape('%h/') + re.escape(relative.as_posix()))
    except ValueError:
        pass
    return re.compile(r'(?P<root>(?:' + '|'.join(forms) + r'))'
                      r'/(?P<layout>current|venv|releases/(?P<pin>[0-9a-f]{40}))'
                      r'/(?:bin|Scripts)/'
                      r'(?P<entry>hub2|hub host)(?=\s|$)')


# The program token follows the marker and its optional systemd prefixes
# (`+`, `!`, `-`; `!!` is covered by the repeated `!`).
_EXECSTART_PROGRAM = re.compile(r'ExecStart=[+!-]*')


def _execstart_program_token(line, pattern):
    """The owned entry match at the ExecStart program-token position, or None."""
    anchor = _EXECSTART_PROGRAM.match(line)
    if anchor is None:
        return None
    return pattern.match(line, anchor.end())


def _verified_pinned_entry(release_directory):
    """The verified legacy-entry flag of a pinned release, or None.

    The contract is derived positively from the pinned release's own
    installed metadata and launcher, never from the current release. It is
    legacy only when that release's own host dist-info declares the private
    `hub2` console entry with the supported target and `bin/hub2` is the
    recognized managed launcher; it is unified only when that dist-info
    positively declares the supported unified `hub` entry (with the
    `wheel_cli` target), `bin/hub2` is absent, and `bin/hub -> ../hub.pyz`
    is the recognized operator launcher (a legacy release ships `bin/hub`
    too, so its presence alone proves nothing, and a lost legacy launcher
    must not become a unified capability). Anything else — missing,
    unreadable, empty, unrelated, malformed or ambiguous metadata, a lost,
    empty or foreign launcher — is an unknown contract and is refused
    unchanged.
    """
    try:
        require_release_directory(release_directory)
    except ValueError:
        return None
    launcher = release_directory / 'bin/hub2'
    if launcher.is_symlink():
        return None
    if launcher.exists():
        # A launcher file only counts as the legacy entry when the release's
        # own wheel metadata declares the entry it runs and the file matches
        # the managed launcher contract.
        if (_release_entry_declaration(release_directory) is True
                and _recognized_legacy_hub2_launcher(launcher, release_directory / 'bin/python')):
            return True
        return None
    entry = release_directory / 'bin/hub'
    if (_release_entry_declaration(release_directory) is False
            and entry.is_symlink() and os.readlink(entry) == '../hub.pyz'
            and (release_directory / 'hub.pyz').is_file()):
        return False
    return None


def align_host_dropins(root):
    """Point hand-made host drop-ins at the entry point the running release has.

    Releases without `bin/hub2` run the daemon as `hub host`; an older retained
    release (rollback/reactivation) still ships `bin/hub2`. Only the ExecStart
    program token directly following the marker and its optional systemd
    prefixes is rewritten — never a path in a foreign program or an argument.
    The remaining argv, all other directives, comments and foreign paths stay
    byte-identical, and a drop-in with multiple or multiline owned execution
    definitions is refused rather than guessed at. A pinned `releases/<id>`
    command follows the verified entry capability of that exact release and
    keeps its pin; the current release's capability is never inferred onto a
    pin, and a pin whose contract cannot be verified is refused unchanged.
    """
    release = (root / 'current').resolve()
    legacy_running = (release / 'bin/hub2').exists()
    pattern = _owned_host_entry_pattern(root)
    directory = Path.home() / '.config/systemd/user' / (UNITS[0] + '.d')
    changed = False
    for path in sorted(directory.glob('*.conf')) if directory.is_dir() else ():
        if path.is_symlink() or not path.is_file():
            continue
        lines = path.read_text().splitlines(keepends=True)
        matches = []
        for index, line in enumerate(lines):
            if not line.startswith('ExecStart='):
                continue
            found = _execstart_program_token(line, pattern)
            if found is None:
                continue
            if line.rstrip('\r\n').endswith('\\'):
                raise ValueError('multiline host ExecStart in ' + path.name)
            matches.append((index, found))
        if len(matches) > 1:
            raise ValueError('ambiguous host ExecStart definitions in ' + path.name)
        updates = []
        for index, matched in matches:
            is_legacy = matched.group('entry') == 'hub2'
            pin = matched.group('pin')
            if pin is None:
                wanted_legacy = legacy_running
            else:
                wanted_legacy = _verified_pinned_entry(root / 'releases' / pin)
                if wanted_legacy is None:
                    raise ValueError('unverifiable pinned release entry contract in ' + path.name)
            if is_legacy == wanted_legacy:
                continue
            base = matched.group(0)[:-(len('hub2') if is_legacy else len('hub host'))]
            updates.append((index, matched.start(), matched.end(),
                            base + ('hub2' if wanted_legacy else 'hub host')))
        if not updates:
            continue
        for index, start, end, replacement in reversed(updates):
            line = lines[index]
            lines[index] = line[:start] + replacement + line[end:]
        durable_write(path, ''.join(lines).encode(), path.stat().st_mode & 0o777)
        changed = True
    if changed:
        command(['systemctl', '--user', 'daemon-reload'])


def restart_host(root):
    align_host_dropins(root)
    command(['systemctl', '--user', 'restart', UNITS[0]])


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
    restart_host(root)
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


def reactivate_retained_release(base, manifest, host_wheel, root, releases, old, target, *, activation_allowed=None):
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
        names = [host_wheel, 'websockets-16.0-py3-none-any.whl', 'hub.pyz', 'hub2-enroll.py']
        if ALERT_CLIENT in manifest['artifacts']:
            names.append(ALERT_CLIENT)
        for name in names:
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
            if activation_allowed is not None and not activation_allowed():
                # No unit or pointer was changed. Restore the retained copy
                # without invoking rollback, which would restart the daemon.
                shutil.rmtree(require_release_directory(target))
                backup.rename(target)
                sync_directory(releases)
                journal.unlink()
                sync_directory(root)
                return False
            if replacements:
                replace_units(units, replacements)
            state['switching'] = True
            durable_write(journal, json.dumps(state, sort_keys=True).encode(), 0o600)
            sync_directory(root)
            link(target, root / 'current')
            restart_host(root)
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
        commit_repin(root, releases)
        repin_managed_profiles(target)
        commit_updater(root, releases)
        finish_retained_switch(root, releases, committed)
        return True


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
                restart_host(root)
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


# Timer checks are cheap; activations are separated by six hours. Busy hosts
# get a further two-hour drain window without resetting it for newer releases.
ACTIVATION_INTERVAL = 6 * 3600
MAX_BUSY_WAIT = 2 * 3600
ACTIVATION_RECEIPT = 'activation-policy.json'


def managed_session_count():
    journal = Path.home() / '.config/agent-hub/v2-journal.sqlite'
    # Read the daemon's authoritative managed-launch journal without creating
    # or migrating it. Unknown state is treated as busy until the bounded wait.
    try:
        with contextlib.closing(sqlite3.connect(journal.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            return db.execute("SELECT count(*) FROM launches WHERE state NOT IN "
                              "('exited','stopped_before_dispatch','launch_failed_reported')").fetchone()[0]
    except (OSError, sqlite3.Error):
        return None


def activation_state(root, current):
    path = root / ACTIVATION_RECEIPT
    state = json.loads(path.read_text()) if path.exists() else {
        'last_activation': current.lstat().st_mtime, 'pending_since': None}
    if not isinstance(state, dict):
        raise ValueError('invalid activation policy receipt')
    for key in ('last_activation', 'pending_since'):
        value = state.get(key)
        if value is None and key == 'pending_since':
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('invalid activation policy timestamp')
    return state


def defer_activation(root, current, version):
    now = time.time()
    state = activation_state(root, current)
    eligible_at = state['last_activation'] + ACTIVATION_INTERVAL
    state.update(version=version, observed_at=now)
    if now < eligible_at:
        state.update(reason='release_batch_interval', next_check_at=eligible_at, managed_sessions=None)
        deferred = True
    else:
        if state['pending_since'] is None:
            state['pending_since'] = now
        count = managed_session_count()
        deadline = state['pending_since'] + MAX_BUSY_WAIT
        deferred = count != 0 and now < deadline
        state.update(managed_sessions=count, next_check_at=min(now + 300, deadline) if deferred else now,
                     reason=('managed_sessions_running' if count is not None else 'managed_sessions_unknown')
                     if deferred else ('bounded_wait_expired' if count != 0 else 'managed_sessions_idle'))
    state['decision'] = 'deferred' if deferred else 'activate'
    write_json(root / ACTIVATION_RECEIPT, state)
    print('Hub2 activation ' + state['decision'] + ': ' + state['reason'] + ' release=' + version)
    return deferred


def record_activation(root, version):
    write_json(root / ACTIVATION_RECEIPT, {
        'last_activation': time.time(), 'pending_since': None, 'version': version,
        'decision': 'activated', 'reason': 'verified_activation'})


# systemd (verified on 255) keeps the last timer activation's TRIGGER_UNIT and
# TRIGGER_TIMER_*_USEC on a later manual `systemctl start`, so a deploy canary's
# explicit start looked timer-triggered and was deferred for hours (intake
# bf07f437). A genuine timer activation execs within moments of its trigger
# time; anything older is a manual start and must apply immediately.
TIMER_TRIGGER_FRESH_S = 15


def timer_triggered(env, now):
    if not env.get('TRIGGER_UNIT', '').endswith('.timer'):
        return False
    raw = env.get('TRIGGER_TIMER_REALTIME_USEC', '')
    if not raw.isdigit():
        return True
    return 0 <= now - int(raw) / 1_000_000 <= TIMER_TRIGGER_FRESH_S


HANDOFF_ARGV = ContextVar('handoff_argv', default=None)


def maybe_handoff_updater(base_url, root, argv):
    """Run the verified candidate updater before prepare_environment when it differs."""
    base = base_url.rstrip('/') + '/cli/'
    manifest = json.loads(fetch(base + 'host-release.json', MAX_MANIFEST))
    validate_manifest(manifest)
    version = manifest['host_version']
    failures = root / 'failed-releases.json'
    record = json.loads(failures.read_text()).get(version, {}) if failures.exists() else {}
    if record.get('retry_at', 0) > time.time():
        return None
    current = root / 'current'
    old = current.resolve(strict=True) if current.is_symlink() else None
    target = root / 'releases' / version
    if old == target and (target / 'release.json').is_file():
        return None
    updater_record = manifest['artifacts']['hub2-self-update.py']
    if matches_installed_artifact(Path(__file__), updater_record):
        return None
    scratch = root / 'tmp'
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='updater-handoff-', dir=scratch) as temporary:
        candidate = Path(temporary) / 'hub2-self-update.py'
        candidate.write_bytes(verified_artifact(base, 'hub2-self-update.py', updater_record))
        candidate.chmod(0o700)
        command([sys.executable, '-m', 'py_compile', candidate])
        command([sys.executable, candidate, '--help'])
        result = subprocess.run([sys.executable, str(candidate), '--no-handoff', *argv])
        return result.returncode


def validate_expected_tuple(expected_host_version, expected_cli_sha256, expected_updater_sha256):
    values = (expected_host_version, expected_cli_sha256, expected_updater_sha256)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError('expected candidate tuple must specify all three values or none')
    if (not isinstance(expected_host_version, str) or not re.fullmatch(r'[0-9a-f]{40}', expected_host_version)
            or not isinstance(expected_cli_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_cli_sha256)
            or not isinstance(expected_updater_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_updater_sha256)):
        raise ValueError('invalid expected candidate tuple format')
    return expected_host_version, expected_cli_sha256, expected_updater_sha256


def validate_expected_manifest(manifest, expected_tuple):
    expected_host, expected_cli, expected_updater = expected_tuple
    artifacts = manifest['artifacts']
    if (manifest['host_version'] != expected_host
            or artifacts['hub.pyz']['sha256'] != expected_cli
            or artifacts['hub2-self-update.py']['sha256'] != expected_updater):
        raise ValueError('published host release does not match expected candidate tuple')


def install(base_url, root, *, install_service=False, restart=True, scheduled=False,
            expected_host_version=None, expected_cli_sha256=None, expected_updater_sha256=None):
    expected_tuple = validate_expected_tuple(
        expected_host_version, expected_cli_sha256, expected_updater_sha256,
    )
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    handoff_argv = HANDOFF_ARGV.get()
    if expected_tuple is None and handoff_argv is not None:
        handed_off = maybe_handoff_updater(base_url, root, handoff_argv)
        if handed_off is not None:
            if handed_off != 0:
                raise RuntimeError(f'candidate updater exited with status {handed_off}')
            return
    with update_lock(root):
        base = base_url.rstrip('/') + '/cli/'
        releases = root / 'releases'
        releases.mkdir(exist_ok=True)
        if expected_tuple is not None:
            manifest = json.loads(fetch(base + 'host-release.json', MAX_MANIFEST))
            host_wheel = validate_manifest(manifest)
            validate_expected_manifest(manifest, expected_tuple)
            recover(root, releases)
        else:
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
            if expected_tuple is not None:
                installed_artifacts = installed.get('artifacts') if isinstance(installed.get('artifacts'), dict) else {}
                installed_cli = installed_artifacts.get('hub.pyz') if isinstance(installed_artifacts.get('hub.pyz'), dict) else {}
                installed_upd = (installed_artifacts.get('hub2-self-update.py')
                                 if isinstance(installed_artifacts.get('hub2-self-update.py'), dict) else {})
                if (installed['host_version'] != expected_tuple[0]
                        or installed_cli.get('sha256') != expected_tuple[1]
                        or installed_upd.get('sha256') != expected_tuple[2]
                        or not matches_installed_artifact(target / 'hub.pyz', manifest['artifacts']['hub.pyz'])):
                    raise ValueError('installed release artifacts do not match expected candidate tuple')
            # Older updaters install the wheel's hub shim even for a release
            # containing this fix. Repair it on the next same-version check,
            # using the original receipt if a rebuild changed zipapp bytes.
            repair_alert_client(base, manifest, installed, target)
            install_operator_cli(target, installed)
            if install_service:
                repair_service_files(base, manifest, root, target)
                if restart:
                    # Explicit setup must load staged/repaired host.env even if
                    # this same release is already running with the old PATH.
                    restart_host(root)
                    check_service_ready(target)
                    record_activation(root, version)
                    enable_service_timer()
            # Heal a pin left on an older release; nothing else to roll back.
            repin_managed_claude(target, base_url.rstrip('/'), root,
                                 lambda: release_updater(base, manifest, root))
            commit_repin(root, releases)
            repin_managed_profiles(target)
            commit_updater(root, releases)
            update_regular_operator_cli(root, target, installed)
            print('Hub2 release already current: ' + version)
            return
        if scheduled and restart and old and defer_activation(root, current, version):
            return
        if target.exists() or target.is_symlink():
            if not restart:
                raise ValueError('retained release activation requires a verified restart')
            allowed = (lambda: not defer_activation(root, current, version)) if scheduled else None
            if not reactivate_retained_release(base, manifest, host_wheel, root, releases, old, target,
                                               activation_allowed=allowed):
                return
            if install_service:
                enable_service_timer()
            prune(root, releases)
            record_activation(root, version)
            update_regular_operator_cli(root, target, read_release_receipt(target))
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
                if expected_tuple is not None:
                    if json.loads(fetch(base + 'host-release.json', MAX_MANIFEST)) != manifest:
                        raise ValueError('published host release changed during candidate preparation')
                # Preparation can take time: recheck just before switching.
                # Returning here removes only our new unactivated environment.
                if scheduled and restart and old and defer_activation(root, current, version):
                    return
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
                    restart_host(root)
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
                            restart_host(root)
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
        commit_repin(root, releases)
        repin_managed_profiles(target)
        commit_updater(root, releases)
        if install_service and restart:
            enable_service_timer()
        prune(root, releases)
        if restart:
            record_activation(root, version)
        update_regular_operator_cli(root, target, read_release_receipt(target))
        print('Activated Hub2 release: ' + version)


def main(argv=None):
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default=os.environ.get('HUB_BASE_URL'))
    parser.add_argument('--root', type=Path, default=Path.home() / '.local/share/agent-hub2')
    parser.add_argument('--install-service', action='store_true', help='Install user units; enable after readiness when restarting')
    parser.add_argument('--expected-host-version', default=None, help='40-hex expected candidate host_version for deploy-gate bootstrap')
    parser.add_argument('--expected-cli-sha256', default=None, help='64-hex expected candidate hub.pyz SHA-256 for deploy-gate bootstrap')
    parser.add_argument('--expected-updater-sha256', default=None, help='64-hex expected candidate hub2-self-update.py SHA-256 for deploy-gate bootstrap')
    parser.add_argument('--no-handoff', action='store_true', help=argparse.SUPPRESS)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--no-restart', action='store_true', help='Stage initial installation before enrollment')
    mode.add_argument('--check-service-ready', action='store_true', help='Verify the started service is connected with its installed release')
    args = parser.parse_args(raw_argv)
    expected_args = (args.expected_host_version, args.expected_cli_sha256, args.expected_updater_sha256)
    if args.check_service_ready and (args.install_service or any(v is not None for v in expected_args)):
        parser.error('--check-service-ready does not install a service or accept expected candidate flags')
    expected_tuple = None
    if any(v is not None for v in expected_args):
        try:
            expected_tuple = validate_expected_tuple(*expected_args)
        except ValueError as exc:
            parser.error(str(exc))
    try:
        if args.check_service_ready:
            check_service_ready(args.root / 'current')
            return 0
        config = Path.home() / '.config/agent-hub/v2.json'
        base = args.base_url or (json.loads(config.read_text()).get('server') if config.exists() else None)
        scheduled = (
            not args.install_service
            and not args.no_restart
            and timer_triggered(os.environ, time.time())
        )
        install_kwargs = {
            'install_service': args.install_service,
            'restart': not args.no_restart,
            'scheduled': scheduled,
        }
        if expected_tuple is not None:
            install_kwargs.update({
                'expected_host_version': expected_tuple[0],
                'expected_cli_sha256': expected_tuple[1],
                'expected_updater_sha256': expected_tuple[2],
            })
        token = HANDOFF_ARGV.set(None if (args.no_handoff or expected_tuple is not None) else raw_argv)
        try:
            install(base or 'https://hub.dtcdev.click', args.root.resolve(), **install_kwargs)
        finally:
            HANDOFF_ARGV.reset(token)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f'Hub2 release update failed ({type(exc).__name__}); inspect the current release and retry. No credentials logged.',
              file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
