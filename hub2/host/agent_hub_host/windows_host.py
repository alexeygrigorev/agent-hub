"""Host lifecycle helpers: loud serve logging, release identity and Windows self-update.

Intake 41ef7048: a Windows Hub2 host refused every launch with a bare reason and
left serve.log empty, because `hub host serve` only logged to an unredirected stderr.
The daemon now owns its log file. On Windows `hub host self-update` installs the
verified published release through the release's own host-install.ps1, which
stops and restarts the AgentHub2Host scheduled task. `self-update` keeps the
installer in the foreground of the same scheduled task and records its exit.
"""
import hashlib
import json
import logging
import logging.handlers
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

LOG_FORMAT = '%(asctime)s %(levelname)s %(name)s %(message)s'
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3
MAX_MANIFEST = 128 * 1024
MAX_INSTALLER = 512 * 1024
INSTALLER = 'host-install.ps1'
SELF_UPDATE_LOG = 'self-update.log'
INSTALL_TIMEOUT_S = 30 * 60
CREDENTIAL_MODES = ('local', 'central')


def state_root():
    """Private per-user Hub2 host directory (%LOCALAPPDATA%\\agent-hub2 on Windows)."""
    base = os.environ.get('LOCALAPPDATA') if os.name == 'nt' else None
    return Path(base or Path.home() / '.local/share') / 'agent-hub2'


def default_log_file():
    # Linux user units already journal stderr; Windows has no service journal.
    return state_root() / 'serve.log' if os.name == 'nt' else None


def configure_logging(log_file=None, stream=None):
    """Log INFO+ to stderr and, when given, a rotating private file. Returns the file path or None."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    formatter = logging.Formatter(LOG_FORMAT)
    if not any(getattr(handler, '_hub2_stderr', False) for handler in root.handlers):
        handler = logging.StreamHandler(stream or sys.stderr)
        handler._hub2_stderr = True
        handler.setFormatter(formatter)
        root.addHandler(handler)
    if log_file is None:
        return None
    path = Path(log_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
                                                       encoding='utf-8', delay=False)
    except OSError as error:
        # The daemon still serves and reports through stderr; say so loudly.
        logging.getLogger(__name__).error('Hub2 serve log unavailable path=%s error=%s', path, type(error).__name__)
        return None
    handler.setFormatter(formatter)
    root.addHandler(handler)
    return path


def host_release():
    """(host_version, protocol_version) of this installed build, or (None, None) when unreleased."""
    try:
        from ._release import HOST_VERSION, PROTOCOL_VERSION
    except (ImportError, SyntaxError):
        return None, None
    if not isinstance(HOST_VERSION, str) or not re.fullmatch(r'[0-9a-f]{40}', HOST_VERSION):
        return None, None
    return HOST_VERSION, PROTOCOL_VERSION


def credential_mode(config):
    launcher = config.get('launcher') or {}
    return 'central' if launcher.get('private_provider_homes', False) else 'local'


def set_credential_mode(config, mode):
    """Return a copy of an enrolled config using host-owned (local) or Hub-delivered (central) logins."""
    if mode not in CREDENTIAL_MODES:
        raise ValueError('invalid_credential_mode')
    if not config.get('machine_id'):
        raise ValueError('host_enrollment_required')
    launcher = dict(config.get('launcher') or {})
    if mode == 'central':
        launcher['private_provider_homes'] = True
    else:
        launcher.pop('private_provider_homes', None)
    updated = dict(config)
    if launcher:
        updated['launcher'] = launcher
    else:
        updated.pop('launcher', None)
    return updated


def _fetch(url, limit, opener=urlopen):
    if urlsplit(url).scheme != 'https':
        raise ValueError('release URL requires HTTPS')
    with opener(url, timeout=30) as response:
        if urlsplit(response.url).scheme != 'https':
            raise ValueError('release redirect requires HTTPS')
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('release artifact exceeds size limit')
    return data


def verified_installer(base_url, opener=urlopen):
    """Download host-install.ps1 and verify it against the published host-release.json."""
    base = base_url.rstrip('/') + '/cli/'
    manifest = json.loads(_fetch(base + 'host-release.json', MAX_MANIFEST, opener))
    version = manifest.get('host_version') if isinstance(manifest, dict) else None
    record = (manifest.get('artifacts') or {}).get(INSTALLER) if isinstance(manifest, dict) else None
    if not isinstance(version, str) or not re.fullmatch(r'[0-9a-f]{40}', version):
        raise ValueError('invalid host version')
    if (not isinstance(record, dict) or not isinstance(record.get('sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', record['sha256'])
            or type(record.get('size')) is not int or not 0 < record['size'] <= MAX_INSTALLER):
        raise ValueError('published release has no Windows host installer')
    data = _fetch(base + INSTALLER, MAX_INSTALLER, opener)
    if len(data) != record['size'] or hashlib.sha256(data).hexdigest() != record['sha256']:
        raise ValueError('Windows host installer checksum mismatch')
    return version, data


def installed_host_version():
    """On-disk release identity, read again after the installer replaced this package under us."""
    sys.modules.pop('agent_hub_host._release', None)
    return host_release()[0]


def _handoff_step(stream, message):
    stream.write((datetime.now(timezone.utc).isoformat(timespec='milliseconds') + ' ' + message + '\n')
                 .encode('utf-8', 'replace'))
    stream.flush()


def _relay_installer_output(process, stream, out):
    """Keep the installer's output in the handoff log and show it to an interactive caller."""
    interactive = bool(getattr(out, 'isatty', lambda: False)())
    for line in iter(process.stdout.readline, b''):
        stream.write(line)
        stream.flush()
        if interactive:
            out.write(line.decode('utf-8', 'replace'))
            out.flush()


def _terminate(process, expired):
    # Mark the deadline before anything is killed: this wait can end as soon as
    # the installer's tree is gone, which is before taskkill.exe returns.
    expired.set()
    try:
        subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        if process.poll() is None:
            process.kill()


def windows_self_update(base_url, force=False, opener=urlopen, spawn=subprocess.Popen, out=sys.stdout):
    """Run the verified installer in update mode, wait for it and return its exit code.

    Intake 56a34448: the daily AgentHub2HostUpdate task reported result 0 while
    nothing was installed. The installer was started with DETACHED_PROCESS;
    Windows PowerShell then exits 0 within a tenth of a second without running
    the script, whether or not its launcher is still alive. Nothing recorded
    that, because its output went to DEVNULL and the launcher returned success
    at once. The launcher now starts the installer with CREATE_NO_WINDOW, keeps
    its output in self-update.log and returns the installer's exit code, so the
    task fails when the install fails. Task Scheduler refuses breakaway from a
    task's job, so the launcher waits in the foreground as the ordinary user.
    """
    version, data = verified_installer(base_url, opener)
    current, _ = host_release()
    if current == version and not force:
        print(json.dumps({'updated': False, 'host_version': version, 'reason': 'already_current'}), file=out)
        return 0
    root = state_root()
    directory = root / 'tmp'
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='host-install-', suffix='.ps1', dir=directory)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
    env = {**os.environ, 'HUB_BASE_URL': base_url.rstrip('/'), 'HUB2_INSTALL_MODE': 'update',
           'HUB2_EXPECTED_HOST_VERSION': version, 'HUB2_UPDATE_FOREGROUND': '1'}
    log = root / SELF_UPDATE_LOG
    started = time.monotonic()
    with open(log, 'ab') as stream:
        _handoff_step(stream, f'self-update start from={current} host_version={version} installer={temporary}')
        process = spawn(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', temporary],
                        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        # The label follows the timer having fired. Timer.finished would not do:
        # it is set only after _terminate returns, and that is later than the
        # moment the killed tree ends this wait.
        expired = threading.Event()
        deadline = threading.Timer(INSTALL_TIMEOUT_S, _terminate, (process, expired))
        deadline.start()
        try:
            _relay_installer_output(process, stream, out)
            code = process.wait()
        finally:
            timed_out = expired.is_set()
            deadline.cancel()
        installed = installed_host_version()
        if timed_out:
            code, reason = code or 1, 'installer_timeout'
        elif code != 0:
            reason = 'installer_failed'
        elif installed != version:
            code, reason = 1, 'installed_version_mismatch'
        else:
            reason = 'installed'
        _handoff_step(stream, f'self-update exit code={code} reason={reason} '
                              f'elapsed_s={round(time.monotonic() - started)} installed_host_version={installed}')
    print(json.dumps({'updated': reason == 'installed', 'from': current, 'host_version': version, 'reason': reason,
                      'exit_code': code, 'installed_host_version': installed, 'log': str(log),
                      'install_log': str(root / 'install.log')}), file=out)
    return code
