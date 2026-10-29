"""Per-user host installer. No privileged package managers or machine settings.

The small shell/PowerShell entrypoints supply uv, Python and a checked zipapp.
This module is also usable for repair on an already enrolled machine.
"""
import argparse
import filecmp
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from contextlib import ExitStack
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import urllib.error
import zipfile
import shlex
import plistlib
import uuid
import tomllib
import stat
from .bootstrap_pins import NODE_SHA256, APLEXER_SHA256, ANTIGRAVITY_RELEASES, ENGINE_PACKAGES, PYTHON_WHEELS


NODE_VERSION = '22.23.4'
APLEXER_VERSION = '0.1.23'
# v0.1.23 omits the previously published macOS assets. Keep each native
# binary and Python client on the same published release for that platform.
APLEXER_MACOS_VERSION = '0.1.22'
GIT_VERSION = '2.56.0.4'
GIT_SHA256 = {
    'x64': 'b1f3e52216e2ed6882d1bebfd4d3fdf319bec40d2edbbf9fd26ec4fd3fce7ed1',
    'arm64': '044110f9d951858452ddb5e4b5415ea0f2b5b2598e794d6fec77fe28b9aa8cc0',
}
SEVENZR_SHA256 = '256feca8e274e5da655e2a284fabafd9f554365eb164862089dacd4e8276d282'
MICROMAMBA_VERSION = '2.10.0'
MICROMAMBA_SHA256 = {
    'linux-64': '9a97976244929bbae28fb1fae504e74a3e7e6d1932870e3a03321f01b7fbd07b',
    'linux-aarch64': '890bc4906e6ab17f1b393f0da21547f75e000078e573a156316ec925b92a4a4c',
    'osx-64': '17b20c4271d65c030a5c86788f5ca86eb575187209f626edabdb8c6a875c6838',
    'osx-arm64': 'acef4eaab19ec2174ad5a449e5aad981ca326270db819502ff096a92fc31caa6',
}
ENGINES = ENGINE_PACKAGES


def user_install_root():
    if sys.platform == 'win32':
        return Path(os.environ['LOCALAPPDATA']) / 'Programs' / 'agent-hub'
    return Path.home() / '.local/share/agent-hub'


def root():
    return persistent_root()


def persistent_root():
    base = user_install_root()
    selected = os.environ.get('HUB_INSTALL_RELEASE') if sys.platform == 'win32' else None
    if selected:
        selected = Path(selected).absolute()
        if (selected.parent != (base / 'releases').absolute() or
                not re.fullmatch(r'[a-f0-9]{16}-[a-f0-9]{12}', selected.name)):
            raise ValueError('Invalid persistent Windows release path')
        if any(p.is_symlink() for p in (base, base / 'releases', selected)):
            raise ValueError('Linked Windows release refused')
        return selected
    return base


def state_root(install_root):
    # Repository work and journals survive an immutable Windows runtime upgrade.
    if sys.platform == 'win32' and Path(install_root).parent == user_install_root() / 'releases':
        return user_install_root()
    return install_root


def python_install_root(install_root):
    return state_root(install_root) / 'python'


def windows_release(archive, base):
    """Reuse completed releases read-only; partial repairs always get a new tree.

    Never truncate a live venv, native daemon binary, alias, or npm engine.
    Previous and interrupted releases remain available for owner-led cleanup.
    """
    from . import private_fs
    digest = hashlib.sha256(Path(archive).read_bytes()).hexdigest()
    releases = base / 'releases'
    if base.is_symlink() or releases.is_symlink():
        raise ValueError('Linked Windows release root refused')
    releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Keep Windows paths short; the completion marker carries the full hash.
    for candidate in sorted(releases.glob(digest[:16] + '-*')):
        if candidate.is_symlink() or not re.fullmatch(digest[:16] + '-[a-f0-9]{12}', candidate.name):
            continue
        try:
            marker = json.loads(private_fs.read_bytes(candidate / 'runtime-ready.json', 256))
            if marker != {'archive_sha256': digest}:
                continue
            python = candidate / 'venv/Scripts/python.exe'
            required = [python, candidate / 'bin/aplexer.exe', candidate / 'bin/a.exe',
                        candidate / 'bin/agy.exe', candidate / 'package/host/agent_hub_host/host_supervisor.py']
            node = candidate / 'runtimes' / ('node-v' + NODE_VERSION + '-win-' + architecture())
            git = candidate / 'runtimes' / ('git-' + GIT_VERSION + '-' + architecture())
            required.extend([node / 'node.exe', node / 'node_modules/npm/bin/npm-cli.js',
                             git / 'cmd/git.exe', git / 'usr/bin/bash.exe'])
            if any(not p.is_file() or p.is_symlink() for p in required):
                continue
            packages_ok = True
            for package in ENGINES.values():
                name, version = package.rsplit('@', 1)
                metadata = candidate / 'engines/node_modules' / name / 'package.json'
                data = json.loads(metadata.read_text())
                if not isinstance(data, dict) or data.get('version') != version:
                    packages_ok = False
                    break
            if not packages_ok:
                continue
            env = dict(os.environ, PYTHONPATH=str(candidate / 'package/host'))
            run([python, '-c', 'import aplexer, quse, agent_hub_host.bootstrap'], env=env, capture=True, timeout=30)
            for binary in (node / 'node.exe', candidate / 'bin/aplexer.exe', candidate / 'bin/agy.exe'):
                run([binary, '--version'], env=env, capture=True, timeout=30)
        except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired):
            continue
        return candidate, True
    candidate = releases / (digest[:16] + '-' + uuid.uuid4().hex[:12])
    candidate.mkdir(mode=0o700)
    return candidate, False


def validate_persistent_install(install_root):
    """Never publish a launcher referring to staging or an ambient interpreter.

    Both wrapper destinations and targets derive from the OS user's durable
    install root. This also catches incomplete test isolation before any
    credentials, executable wrappers or services can be changed.
    """
    expected = persistent_root()
    install_root = Path(install_root).absolute()
    home = Path.home().absolute()
    if sys.platform == 'win32':
        for field in ('LOCALAPPDATA', 'APPDATA'):
            value = Path(os.environ.get(field, '')).absolute()
            if not value.is_relative_to(home):
                raise ValueError('Refusing split Windows user profile paths')
    if install_root != expected.absolute():
        raise ValueError('Refusing launcher outside the persistent per-user Hub installation')
    python = install_root / ('venv/Scripts/python.exe' if sys.platform == 'win32' else 'venv/bin/python')
    if Path(sys.executable).absolute() != python.absolute() or not python.is_file():
        raise ValueError('Refusing launcher using a temporary or ambient Python interpreter')
    package = install_root / 'package/host/agent_hub_host'
    if any(not (package / name).is_file() for name in ('cli.py', 'host_supervisor.py')):
        raise ValueError('Persistent Hub package is incomplete; launcher preserved')
    for path in (home / '.local', home / '.local/share', home / '.local/bin',
                 install_root, install_root / 'bin', install_root / 'venv', python.parent,
                 python_install_root(install_root), install_root / 'package', package.parent, package):
        if path.is_symlink():
            raise ValueError('Refusing a linked Hub installation')
    # A durable venv path can still redirect to a disposable base interpreter.
    # Check the running interpreter's base too (Windows venv launchers are
    # regular files, so checking only a POSIX symlink target is insufficient).
    base = Path(sys.base_prefix).resolve()
    if not base.is_dir() or not base.is_relative_to(python_install_root(install_root).resolve()):
        raise ValueError('Refusing launcher using Python outside the persistent managed runtime')


def validate_side_effect_context(install_root):
    """An overridden HOME must never reconfigure the live user's OS services."""
    if sys.platform == 'win32':
        import winreg
        from .win32_fs import api
        key = r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList' + '\\' + api().sid_text
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as profile:
            native_home = Path(os.path.expandvars(winreg.QueryValueEx(profile, 'ProfileImagePath')[0]))
    else:
        import pwd
        native_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    if Path.home().resolve() != native_home.resolve():
        raise ValueError('Refusing service or registry changes for an overridden user profile')
    temporary = [Path('/tmp'), Path('/var/tmp'), Path('/private/tmp'),
                 Path(tempfile.gettempdir()), native_home / '.local/tmp']
    if sys.platform == 'win32':
        temporary.append(native_home / 'AppData/Local/Temp')
    if any(Path(install_root).resolve().is_relative_to(p.resolve()) for p in temporary):
        raise ValueError('Refusing service or registry launchers in temporary storage')


def architecture():
    # Python may be x64 under Windows ARM emulation. Native runtime archives
    # must use the hardware architecture rather than that of Python.
    from .system import get_arch
    cpu = get_arch()
    aliases = {'aarch64': 'arm64', 'arm64': 'arm64', 'amd64': 'x64', 'x86_64': 'x64'}
    if cpu not in aliases:
        raise RuntimeError('Unsupported host architecture: ' + cpu)
    return aliases[cpu]


def run(argv, *, env=None, capture=False, timeout=900):
    result = subprocess.run([str(x) for x in argv], env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None,
                            text=True, timeout=timeout)
    if result.returncode:
        # Never propagate provider output, auth data or command arguments.
        raise RuntimeError('Host setup step failed: ' + Path(argv[0]).name)
    return result.stdout.strip() if capture else None


def download(url, target):
    if not url.startswith('https://'):
        raise ValueError('Runtime downloads require HTTPS')
    req = urllib.request.Request(url, headers={'User-Agent': 'agent-hub-installer/1'})
    with urllib.request.urlopen(req, timeout=120) as source, Path(target).open('wb') as out:
        shutil.copyfileobj(source, out)


def verify(path, sums, name):
    matches = [line.split()[0] for line in sums.splitlines()
               if len(line.split()) == 2 and line.split()[1].lstrip('*') == name]
    if len(matches) != 1 or not re.fullmatch('[0-9a-fA-F]{64}', matches[0]):
        raise ValueError('Missing or ambiguous runtime checksum')
    with Path(path).open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != matches[0].lower():
        raise ValueError('Runtime checksum mismatch')


def unpack(archive, destination):
    """Reject path traversal, symlinks and device nodes in runtime archives."""
    destination = Path(destination)
    def safe(name):
        p = PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name:
            raise ValueError('Unsafe runtime archive path')
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for item in z.infolist():
                safe(item.filename)
                if (item.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Runtime archive contains a link')
            z.extractall(destination)
    else:
        with tarfile.open(archive) as t:
            for item in t.getmembers():
                safe(item.name)
                if not (item.isfile() or item.isdir()):
                    # Node includes npm symlinks. We invoke npm-cli.js with
                    # node directly, so no links need to be extracted.
                    if item.issym() or item.islnk():
                        continue
                    raise ValueError('Runtime archive contains a special file')
                target = destination / item.name
                if item.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with t.extractfile(item) as source, target.open('wb') as out:
                        shutil.copyfileobj(source, out)
                    target.chmod(item.mode & 0o777)


def ensure_node(install_root):
    system = {'linux': 'linux', 'darwin': 'darwin', 'win32': 'win'}[sys.platform]
    name = 'node-v' + NODE_VERSION + '-' + system + '-' + architecture()
    target = install_root / 'runtimes' / name
    node = target / ('node.exe' if sys.platform == 'win32' else 'bin/node')
    npm = target / ('node_modules/npm/bin/npm-cli.js' if sys.platform == 'win32'
                    else 'lib/node_modules/npm/bin/npm-cli.js')
    if not node.is_file() or not npm.is_file():
        extension = '.zip' if sys.platform == 'win32' else ('.tar.xz' if sys.platform == 'linux' else '.tar.gz')
        asset = name + extension
        base = 'https://nodejs.org/dist/v' + NODE_VERSION + '/'
        with tempfile.TemporaryDirectory(dir=install_root / 'tmp') as tmp:
            archive = Path(tmp) / asset
            download(base + asset, archive)
            verify(archive, NODE_SHA256[asset] + '  ' + asset, asset)
            unpack(archive, Path(tmp) / 'expanded')
            target.parent.mkdir(parents=True, exist_ok=True)
            # Repair a partially installed version in place. shutil.move
            # into an existing directory would nest it and remain broken.
            shutil.copytree(Path(tmp) / 'expanded' / name, target, dirs_exist_ok=True)
    run([node, '--version'], capture=True)
    if sys.platform != 'win32':
        # Runtime extraction deliberately skips links. Replace Node's npm/npx
        # symlinks with durable launchers needed by managed provisioning too.
        for command in ('npm', 'npx'):
            script = node.parent / command
            cli = npm.with_name(command + '-cli.js')
            write_private(script, '#!/bin/sh\nexec ' + shlex.quote(str(node)) + ' ' +
                          shlex.quote(str(cli)) + ' "$@"\n')
            script.chmod(0o755)
    return node, npm


def aplexer_version():
    return APLEXER_MACOS_VERSION if sys.platform == 'darwin' else APLEXER_VERSION


def ensure_aplexer(install_root):
    binary_dir = install_root / 'bin'
    suffix = '.exe' if sys.platform == 'win32' else ''
    binary = binary_dir / ('aplexer' + suffix)
    system = {'win32': 'windows', 'darwin': 'macos', 'linux': 'linux'}[sys.platform]
    cpu = 'amd64' if architecture() == 'x64' else 'arm64'
    asset = 'aplexer-' + system + '-' + cpu + suffix
    version = aplexer_version()
    base = 'https://github.com/PocketShell-io/aplexer/releases/download/v' + version + '/'
    stamp = binary_dir / 'aplexer-version'
    if not binary.is_file() or not stamp.is_file() or stamp.read_text() != version:
        with tempfile.TemporaryDirectory(dir=install_root / 'tmp') as tmp:
            staged = Path(tmp) / asset
            download(base + asset, staged)
            verify(staged, APLEXER_SHA256[version][asset] + '  ' + asset, asset)
            staged.chmod(0o755)
            run([staged, '--version'], capture=True)
            os.replace(staged, binary)
            stamp.write_text(version)
    alias = binary_dir / ('a' + suffix)
    # Keep the `a` used by hooks consistent after upgrades and repairs.
    # Stage the alias too: an interrupted copy must not
    # truncate the existing executable, and unchanged Windows binaries may
    # still be in use and must not be replaced unnecessarily.
    if alias.is_symlink() or not alias.is_file() or not filecmp.cmp(binary, alias, shallow=False):
        fd, staging = tempfile.mkstemp(prefix='.a-', suffix=suffix, dir=binary_dir)
        os.close(fd)
        try:
            shutil.copy2(binary, staging)
            os.replace(staging, alias)
        finally:
            if os.path.exists(staging):
                os.unlink(staging)
    return binary


def install_engines(install_root, node, npm):
    prefix = install_root / 'engines'
    env = dict(os.environ, npm_config_prefix=str(prefix),
               npm_config_cache=str(install_root / 'cache/npm'))
    engine_bin = prefix if sys.platform == 'win32' else prefix / 'bin'
    env['PATH'] = os.pathsep.join([str(node.parent), str(engine_bin), env.get('PATH', '')])
    # npm lifecycle scripts are required by native engine packaging; execute
    # only the explicit engine package allowlist, never a remotely supplied package.
    run([node, npm, 'install', '--global', '--prefix', prefix, '--no-audit', '--no-fund',
         '--', *ENGINES.values()], env=env)
    return env


def ensure_windows_git(install_root):
    """Extract PortableGit; never execute its installer or request elevation."""
    if sys.platform != 'win32':
        return None
    target = install_root / 'runtimes' / ('git-' + GIT_VERSION + '-' + architecture())
    git, bash = target / 'cmd/git.exe', target / 'usr/bin/bash.exe'
    if not git.is_file() or not bash.is_file():
        cpu = '64-bit' if architecture() == 'x64' else 'arm64'
        name = 'PortableGit-' + GIT_VERSION + '-' + cpu + '.7z.exe'
        base = 'https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.4/'
        with tempfile.TemporaryDirectory(dir=install_root / 'tmp') as tmp:
            archive, extractor = Path(tmp) / name, Path(tmp) / '7zr.exe'
            download(base + name, archive)
            verify(archive, GIT_SHA256[architecture()] + '  ' + name, name)
            download('https://www.7-zip.org/a/7zr.exe', extractor)
            verify(extractor, SEVENZR_SHA256 + '  7zr.exe', '7zr.exe')
            expanded = Path(tmp) / 'expanded'
            run([extractor, 'x', '-y', '-o' + str(expanded), archive], capture=True)
            if not (expanded / 'cmd/git.exe').is_file() or not (expanded / 'usr/bin/bash.exe').is_file():
                raise ValueError('Portable Git archive is incomplete')
            shutil.copytree(expanded, target, dirs_exist_ok=True)
    run([git, '--version'], capture=True)
    os.environ['CLAUDE_CODE_GIT_BASH_PATH'] = str(bash)
    os.environ['PATH'] = str(git.parent) + os.pathsep + os.environ.get('PATH', '')
    return target


def ensure_git(install_root):
    if sys.platform == 'win32':
        return ensure_windows_git(install_root)
    # Avoid macOS's /usr/bin/git stub, which opens an Xcode installation
    # prompt. A user-owned Git distribution also works on bare Linux hosts.
    target = install_root / 'runtimes/git'
    git = target / 'bin/git'
    if not git.is_file():
        subdir = (('linux-64' if architecture() == 'x64' else 'linux-aarch64')
                  if sys.platform == 'linux' else
                  ('osx-64' if architecture() == 'x64' else 'osx-arm64'))
        name = 'micromamba-' + MICROMAMBA_VERSION + '-0.tar.bz2'
        url = ('https://api.anaconda.org/download/conda-forge/micromamba/' +
               MICROMAMBA_VERSION + '/' + subdir + '/' + name)
        with tempfile.TemporaryDirectory(dir=install_root / 'tmp') as tmp:
            archive = Path(tmp) / name
            download(url, archive)
            verify(archive, MICROMAMBA_SHA256[subdir] + '  ' + name, name)
            unpack(archive, Path(tmp) / 'expanded')
            run([Path(tmp) / 'expanded/bin/micromamba', '--no-rc',
                 '--root-prefix', install_root / 'cache/mamba', 'create', '--yes',
                 '--override-channels', '--channel', 'conda-forge',
                 '--prefix', target, 'git'], capture=True)
    run([git, '--version'], capture=True)
    os.environ['PATH'] = str(git.parent) + os.pathsep + os.environ.get('PATH', '')
    return target


def ensure_antigravity(install_root):
    """Use Google's public updater manifest, the same source as its installer."""
    system = {'linux': 'linux', 'darwin': 'darwin', 'win32': 'windows'}[sys.platform]
    cpu = 'amd64' if architecture() == 'x64' else 'arm64'
    data = ANTIGRAVITY_RELEASES[system + '_' + cpu]
    binary = install_root / 'bin' / ('agy.exe' if sys.platform == 'win32' else 'agy')
    stamp = install_root / 'bin/agy-version'
    with tempfile.TemporaryDirectory(dir=install_root / 'tmp') as tmp:
        if (not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', data.get('version', '')) or
                not re.fullmatch('[a-fA-F0-9]{128}', data.get('sha512', '')) or
                not data.get('url', '').startswith('https://storage.googleapis.com/antigravity-public/antigravity-cli/')):
            raise ValueError('Invalid Antigravity release manifest')
        if binary.is_file() and stamp.is_file() and stamp.read_text() == data['version']:
            return binary
        payload = Path(tmp) / 'payload'
        download(data['url'], payload)
        with payload.open('rb') as source:
            actual = hashlib.file_digest(source, 'sha512').hexdigest()
        if actual != data['sha512'].lower():
            raise ValueError('Antigravity checksum mismatch')
        if data['url'].endswith('.tar.gz'):
            unpack(payload, Path(tmp) / 'expanded')
            payload = Path(tmp) / 'expanded/antigravity'
        payload.chmod(0o755)
        run([payload, '--version'], capture=True)
        os.replace(payload, binary)
        stamp.write_text(data['version'])
    return binary


def install_package(archive, install_root, uv):
    venv = install_root / 'venv'
    python = venv / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    env = dict(os.environ)
    env['UV_PYTHON_INSTALL_DIR'] = str(python_install_root(install_root))
    env['UV_PYTHON_DOWNLOADS'] = 'automatic'
    env.pop('UV_NO_MANAGED_PYTHON', None)
    # Never inherit the bootstrap interpreter: it may come from an activated
    # temporary venv. Reapplying also repairs an existing venv's stale base.
    requested = 'cpython-3.12-windows-x86_64' if sys.platform == 'win32' else '3.12'
    run([uv, 'venv', '--allow-existing', '--managed-python', '--python', requested, venv], env=env)
    # Pin the complete runtime dependency set, including platform-specific
    # native wheels. No live registry metadata determines installation content.
    requirements = []
    for name in ('quse', 'click', 'colorama'):
        wheel = next(iter(PYTHON_WHEELS[name]['wheels'].values()))
        requirements.append(wheel['url'] + ' --hash=sha256:' + wheel['sha256'])
    if sys.platform == 'darwin':
        cpu = 'x86_64' if architecture() == 'x64' else 'arm64'
        name = 'aplexer_client-' + aplexer_version() + '-cp311-abi3-macosx_11_0_' + cpu + '.whl'
        url = 'https://github.com/PocketShell-io/aplexer/releases/download/v' + aplexer_version() + '/' + name
        checksum = APLEXER_SHA256[aplexer_version()][name]
    else:
        suffix = ('win_amd64.whl' if sys.platform == 'win32' else
                  'manylinux_2_28_' + ('x86_64' if architecture() == 'x64' else 'aarch64') + '.whl')
        wheel = next(w for n, w in PYTHON_WHEELS['aplexer-client']['wheels'].items() if n.endswith(suffix))
        url, checksum = wheel['url'], wheel['sha256']
    requirements.append(url + ' --hash=sha256:' + checksum)
    (install_root / 'tmp').mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=install_root / 'tmp') as tmp:
        path = Path(tmp) / 'runtime-requirements.txt'
        path.write_text('\n'.join(requirements) + '\n')
        run([uv, 'pip', 'install', '--python', python, '--require-hashes', '--no-deps', '-r', path])
    # Keep the maintained supervisor's checkout contract: workspace/host is
    # the Python import root for its native controller and all descendants.
    package = install_root / 'package/host'
    package.parent.mkdir(parents=True, exist_ok=True)
    # Extraction must finish before replacing any installed source. Keep the
    # old tree beside the staged tree until publication succeeds, so an I/O
    # error can restore the complete previous package.
    with tempfile.TemporaryDirectory(prefix='.package-', dir=package.parent) as tmp:
        staged = Path(tmp) / 'host'
        # Outside the staging cleanup: even a failed rollback must retain
        # the previous source for recovery rather than deleting it on exit.
        previous = package.parent / ('.host-previous-' + uuid.uuid4().hex)
        staged.mkdir()
        unpack(archive, staged)
        moved_previous = False
        if package.exists():
            os.replace(package, previous)
            moved_previous = True
        try:
            os.replace(staged, package)
        except BaseException:
            if moved_previous:
                os.replace(previous, package)
            raise
        if moved_previous:
            shutil.rmtree(previous)
    return python, package


def write_private(path, text, *, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Publish atomically and never follow an existing launcher symlink.
    fd, staging = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            if mode is not None:
                os.fchmod(out.fileno(), mode)
            out.write(text)
        os.replace(staging, path)
    finally:
        if os.path.exists(staging):
            os.unlink(staging)


def service_command(install_root, settings):
    command = [sys.executable, '-u', '-m', 'agent_hub_host.host_supervisor',
            '--workspace', str(install_root / 'package'), '--config', str(settings),
            '--credential-config', str(__import__('agent_hub_host.config', fromlist=['']).get_host_config_path()),
            '--journal-dir', str(state_root(install_root) / 'journal')]
    if sys.platform == 'win32':
        command.extend(['--control-dir', str(state_root(install_root) / 'controller')])
    return command


def runtime_directories(install_root):
    node_dir = install_root / 'runtimes' / ('node-v' + NODE_VERSION + '-' +
                {'linux': 'linux', 'darwin': 'darwin', 'win32': 'win'}[sys.platform] + '-' + architecture())
    directories = [install_root / 'bin',
                   install_root / ('venv/Scripts' if sys.platform == 'win32' else 'venv/bin'),
                   install_root / ('engines' if sys.platform == 'win32' else 'engines/bin'),
                   node_dir if sys.platform == 'win32' else node_dir / 'bin']
    if sys.platform == 'win32':
        git_root = install_root / 'runtimes' / ('git-' + GIT_VERSION + '-' + architecture())
        directories.append(git_root / 'cmd')
    else:
        directories.append(install_root / 'runtimes/git/bin')
        directories.append(Path.home() / '.local/bin')
    return directories


def publish_user_path(install_root):
    """Expose the same per-user engines and quota CLI to humans and services."""
    directories = runtime_directories(install_root)
    if sys.platform == 'win32':
        git_root = install_root / 'runtimes' / ('git-' + GIT_VERSION + '-' + architecture())
        os.environ['CLAUDE_CODE_GIT_BASH_PATH'] = str(git_root / 'usr/bin/bash.exe')
        quote = lambda s: "'" + str(s).replace("'", "''") + "'"
        ps = ('$old=[Environment]::GetEnvironmentVariable("Path","User");'
              '$add=@(' + ','.join(quote(p) for p in directories) + ');'
              '$parts=@($add)+@($old -split ";" | Where-Object { $_ -and $_ -notin $add });'
              '[Environment]::SetEnvironmentVariable("Path",($parts -join ";"),"User");'
              '[Environment]::SetEnvironmentVariable("CLAUDE_CODE_GIT_BASH_PATH",' +
              quote(git_root / 'usr/bin/bash.exe') + ',"User")')
        run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', ps])
    else:
        path_script = install_root / 'path.sh'
        write_private(path_script, 'export PATH=' + shlex.quote(':'.join(map(str, directories))) + ':"$PATH"\n')
        include = '\n# Agent Hub per-user runtimes\n. ' + shlex.quote(str(path_script)) + '\n'
        for name in ('.profile', '.zprofile', '.zshrc', '.bashrc'):
            profile = Path.home() / name
            # Preserve shell profiles and refuse linked files outside HOME.
            if profile.is_symlink():
                continue
            old = profile.read_text() if profile.exists() else ''
            if include not in old:
                mode = stat.S_IMODE(profile.stat().st_mode) if profile.exists() else None
                write_private(profile, old + include, mode=mode)
    os.environ['PATH'] = os.pathsep.join([*(str(p) for p in directories), os.environ.get('PATH', '')])


def configure_default_engine():
    """Apply the requested Claude Opus default without replacing other profiles."""
    from .provisioning import set_toml_top_level_keys
    config_dir = (Path(os.environ['APPDATA']) if sys.platform == 'win32' else
                  Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))))
    native = config_dir / 'aplexer/config.toml'
    settings = Path.home() / '.claude/settings.json'
    if any(p.is_symlink() for p in (native, native.parent, settings, settings.parent)):
        raise ValueError('Refusing linked engine configuration')
    content = native.read_text() if native.exists() else 'version = 1\n'
    if 'default_engine' not in tomllib.loads(content):
        write_private(native, set_toml_top_level_keys(content, {'default_engine': 'claude'}))
    data = json.loads(settings.read_text()) if settings.exists() else {}
    if not isinstance(data, dict):
        raise ValueError('Invalid Claude settings; existing file preserved')
    data.setdefault('model', 'opus')
    write_private(settings, json.dumps(data, indent=2) + '\n')


def install_service(install_root, command):
    """Install only this user's named controller; never stop unrelated services."""
    log = state_root(install_root) / 'controller.log'
    # Native controller sessions inherit only non-secret bootstrap paths.
    if sys.platform == 'win32':
        system = Path(os.environ['SystemRoot'])
        system_dirs = [system / 'System32', system, system / 'System32/Wbem',
                       system / 'System32/WindowsPowerShell/v1.0']
    else:
        system_dirs = [Path('/usr/local/bin'), Path('/usr/bin'), Path('/bin')]
    env = {'PATH': os.pathsep.join(map(str, [*runtime_directories(install_root), *system_dirs])),
           'PYTHONPATH': str(install_root / 'package/host'),
           'UV_PYTHON_INSTALL_DIR': str(python_install_root(install_root))}
    if sys.platform == 'win32':
        env['CLAUDE_CODE_GIT_BASH_PATH'] = str(install_root / 'runtimes' /
            ('git-' + GIT_VERSION + '-' + architecture()) / 'usr/bin/bash.exe')
    if sys.platform == 'win32':
        from .bootstrap_control import handover
        handover(state_root(install_root) / 'controller')
        runner = install_root / 'controller.ps1'
        quote = lambda s: "'" + str(s).replace("'", "''") + "'"
        lines = ['$ErrorActionPreference = \'Stop\'']
        lines.extend('$env:' + k + ' = ' + quote(v) for k, v in env.items())
        lines.extend(['Set-Location -LiteralPath ' + quote(install_root / 'package'),
                      '& ' + ' '.join(quote(x) for x in command) + ' >> ' + quote(log) + ' 2>&1',
                      'exit $LASTEXITCODE'])
        write_private(runner, '\n'.join(lines))
        # A per-user logon task requires no password, RunAs or UAC. Task names
        # include the user SID so reruns reuse exactly one registration.
        ps = (' $id=[Security.Principal.WindowsIdentity]::GetCurrent();'
              ' $name="AgentHubController-"+$id.User.Value;'
              ' $old=Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue;'
              ' $until=(Get-Date).AddSeconds(45);'
              ' while($old -and $old.State -eq "Running" -and (Get-Date) -lt $until) {'
              ' Start-Sleep -Milliseconds 500; $old=Get-ScheduledTask -TaskName $name; };'
              ' if($old -and $old.State -eq "Running") { throw "Existing controller has not exited; preserved"; };'
              ' $a=New-ScheduledTaskAction -Execute "powershell.exe" -Argument '
              + quote('-NoProfile -ExecutionPolicy Bypass -File "' + str(runner) + '"') + ';'
              ' $t=New-ScheduledTaskTrigger -AtLogOn -User $id.Name;'
              ' $p=New-ScheduledTaskPrincipal -UserId $id.Name -LogonType Interactive -RunLevel Limited;'
              ' $s=New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero)'
              ' -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew'
              ' -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable;'
              ' Register-ScheduledTask -TaskName $name -Action $a -Trigger $t -Principal $p -Settings $s -Force | Out-Null;'
              ' Start-ScheduledTask -TaskName $name;')
        run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', "$ErrorActionPreference='Stop';" + ps])
        return 'windows_user_logon_task'
    if sys.platform == 'darwin':
        path = Path.home() / 'Library/LaunchAgents/click.dtcdev.agent-hub.plist'
        body = {'Label': 'click.dtcdev.agent-hub', 'ProgramArguments': command,
                'WorkingDirectory': str(install_root / 'package'), 'RunAtLoad': True,
                'KeepAlive': True, 'EnvironmentVariables': env,
                'StandardOutPath': str(log), 'StandardErrorPath': str(log)}
        write_private(path, plistlib.dumps(body).decode())
        domain = 'gui/' + str(os.getuid())
        probe = subprocess.run(['launchctl', 'print', domain + '/click.dtcdev.agent-hub'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not probe.returncode:
            run(['launchctl', 'bootout', domain, path])
        run(['launchctl', 'bootstrap', domain, path])
        run(['launchctl', 'kickstart', '-k', domain + '/click.dtcdev.agent-hub'])
        return 'launch_agent'
    def systemd_quote(value):
        if any(c in value for c in ('\n', '\r', '\0')):
            raise ValueError('Invalid service path')
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'
    unit = Path.home() / '.config/systemd/user/agent-hub-controller.service'
    lines = ['[Unit]', 'Description=Agent Hub user controller', 'After=network-online.target',
             '[Service]', 'Type=simple', 'WorkingDirectory=' + systemd_quote(str(install_root / 'package')),
             'ExecStart=' + ' '.join(systemd_quote(str(x).replace('$', '$$')) for x in command),
             'Restart=on-failure', 'RestartSec=10', 'TimeoutStopSec=45']
    lines.extend('Environment=' + systemd_quote(k + '=' + v) for k, v in env.items())
    lines.extend(['[Install]', 'WantedBy=default.target', ''])
    write_private(unit, '\n'.join(lines))
    run(['systemctl', '--user', 'daemon-reload'])
    run(['systemctl', '--user', 'enable', 'agent-hub-controller.service'])
    # An existing enabled service needs a restart to load repaired runtimes
    # and environment. enable --now would leave the previous process alive.
    run(['systemctl', '--user', 'restart', 'agent-hub-controller.service'])
    return 'systemd_user'


def configure(base_url):
    from . import api, auth, config, fleet, provisioning, system
    install_root = root()
    validate_persistent_install(install_root)
    validate_side_effect_context(install_root)
    # An operator may have exported the human CLI profile. Host enrollment
    # must select its own durable file before reading or saving credentials.
    config_root = (Path(os.environ['APPDATA']) if sys.platform == 'win32' else
                   Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))))
    os.environ['HUB_CONFIG_FILE'] = str(config_root / 'agent-hub/host.json')
    cfg = config.load_host_config()
    if cfg.get('base_url') and not config.same_origin(base_url, cfg['base_url']):
        raise ValueError('Existing host belongs to another Hub; no credentials changed')
    if not cfg.get('access_token'):
        auth.device_login(base_url, 'host', 'agent-hub-host', system.get_hostname(),
                          auth.HOST_SCOPES + ['repositories:read'])
    api.register_machine(base_url)
    settings = fleet.subscriptions_path()
    if not settings.exists():
        write_private(settings, json.dumps({'version': 1, 'subscriptions': [],
                      'capabilities': [], 'max_agents': 1,
                      'workspaces': {'home': str(Path.home())}}))
    fleet.load_settings(settings)  # Validate, never overwrite local operator settings.
    publish_user_path(install_root)
    configure_default_engine()
    from . import enrollment_repositories
    paired = config.load_host_config()
    repositories = None
    repository_access = 'not_configured'
    granted = paired.get('granted_scopes')
    requested = paired.get('requested_scopes', [])
    if isinstance(granted, list):
        repository_access = 'declined' if 'repositories:read' in requested else 'not_requested'
        repository_allowed = 'repositories:read' in granted
    else:
        # Older profiles did not save granted scopes. Probe with the host
        # credential; the server still enforces current consent and family.
        repository_allowed = 'repositories:read' in requested
    if repository_allowed:
        try:
            repositories = enrollment_repositories.sync(state_root(install_root))
        except urllib.error.HTTPError as error:
            if error.code == 403:
                repository_access = 'forbidden'
            elif error.code in api.HUB_UNAVAILABLE_STATUS or error.code == 429:
                repository_access = 'unavailable'
            else:
                raise
            # Optional repository consent must not strand an enrolled host
            # before its controller is installed, including on a repair run.
        except (urllib.error.URLError, TimeoutError):
            repository_access = 'unavailable'
        else:
            repository_access = 'configured' if repositories is not None else 'not_configured'
        if repositories:
            data = json.loads(settings.read_text())
            workspaces = data.setdefault('workspaces', {})
            for alias, path in repositories.items():
                if alias in workspaces and workspaces[alias] != path:
                    raise ValueError('Existing repository workspace differs; settings preserved')
                workspaces[alias] = path
            write_private(settings, json.dumps(data, indent=2))
            fleet.load_settings(settings)
    # Keep the runtime PATH usable for human commands and all service children.
    bin_dir = install_root / 'bin'
    if sys.platform == 'win32':
        batch = lambda value: str(value).replace('%', '%%')
        launcher = '@echo off\r\n"' + batch(sys.executable) + '" -m agent_hub_host.cli %*\r\n'
        # PYTHONPATH is inherited by service children; human launcher embeds
        # its own package path rather than depending on a shell profile.
        launcher = launcher.replace('@echo off\r\n', '@echo off\r\nsetlocal\r\nset "PYTHONPATH=' + batch(install_root / 'package/host') + '"\r\n' +
                   'set "PATH=' + ';'.join(batch(p) for p in runtime_directories(install_root)) + ';%PATH%"\r\n' +
                   'set "CLAUDE_CODE_GIT_BASH_PATH=' + batch(os.environ['CLAUDE_CODE_GIT_BASH_PATH']) + '"\r\n')
        write_private(bin_dir / 'hub.cmd', launcher)
    else:
        human_bin = Path.home() / '.local/bin'
        human_bin.mkdir(parents=True, exist_ok=True)
        wrapper = '#!/bin/sh\n. ' + shlex.quote(str(install_root / 'path.sh')) + '\nexport PYTHONPATH=' + shlex.quote(str(install_root / 'package/host')) + '\nexec ' + shlex.quote(sys.executable) + ' -m agent_hub_host.cli "$@"\n'
        write_private(human_bin / 'hub', wrapper)
        (human_bin / 'hub').chmod(0o755)
    print('Host enrolled; reconciling centrally managed engine accounts.', flush=True)
    provisioning.sync()
    service = install_service(install_root, service_command(install_root, settings))
    print(json.dumps({'status': 'installed', 'machine_id': config.load_host_config().get('machine_id'),
                      'service': service, 'elevation_prompts': 0,
                      'repository_access': repository_access}), flush=True)


def main(argv=None):
    if argv is None and len(sys.argv) == 3 and sys.argv[1] == '--configure':
        return configure(sys.argv[2])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--base-url', default='https://hub.dtcdev.click')
    parser.add_argument('--uv', required=True, type=Path)
    args = parser.parse_args(argv)
    from .api import check_url
    check_url(args.base_url)
    from . import private_fs
    with ExitStack() as stack:
        if sys.platform == 'win32':
            stack.enter_context(private_fs.exclusive_lock(user_install_root() / 'locks/bootstrap.lock'))
        _install(args)


def _install(args):
    # Ignore an ambient release selection; the verified archive chooses it.
    os.environ.pop('HUB_INSTALL_RELEASE', None)
    install_root = user_install_root()
    ready = False
    if sys.platform == 'win32':
        install_root, ready = windows_release(args.archive, install_root)
        os.environ['HUB_INSTALL_RELEASE'] = str(install_root)
    for name in ('bin', 'tmp', 'cache'):
        (install_root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    print('Installing runtimes in user scope (0 elevation prompts).', flush=True)
    if ready:
        python = install_root / 'venv/Scripts/python.exe'
        package = install_root / 'package/host'
        env = dict(os.environ)
    else:
        node, npm = ensure_node(install_root)
        ensure_aplexer(install_root)
        python, package = install_package(args.archive, install_root, args.uv)
        ensure_git(install_root)
        env = install_engines(install_root, node, npm)
        ensure_antigravity(install_root)
        if sys.platform == 'win32':
            from . import private_fs
            digest = hashlib.sha256(args.archive.read_bytes()).hexdigest()
            private_fs.atomic_write(install_root / 'runtime-ready.json',
                                    json.dumps({'archive_sha256': digest}).encode(), 256)
    env['PYTHONPATH'] = str(package)
    env['PATH'] = os.pathsep.join([str(install_root / 'bin'), str(python.parent), env['PATH']])
    # Continue in the dedicated interpreter so installed native client/quota
    # dependencies and the extracted final package are available.
    run([python, '-m', 'agent_hub_host.bootstrap', '--configure', args.base_url], env=env)


if __name__ == '__main__':
    main()
