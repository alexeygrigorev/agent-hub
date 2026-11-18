"""`hub host ...`: host-daemon administration inside the single `hub` CLI.

The daemon (enroll, serve, heartbeat, self-update, ...) lives in the managed
release venv, whose `agent_hub_host` package has the same name as this operator
CLI. It cannot be imported here, so `hub host` replaces this process with the
release interpreter running the daemon module. There is no separate CLI name.
"""
import os
import subprocess
import sys
from pathlib import Path

DAEMON_ARGS = ('-I', '-m', 'agent_hub_host.cli')


def _python_in(release):
    for relative in ('bin/python', 'Scripts/python.exe'):
        candidate = Path(release) / relative
        if candidate.is_file():
            return candidate
    return None


def daemon_python(archive=None, env=None):
    """Release venv interpreter next to this zipapp, else the installed current release."""
    env = os.environ if env is None else env
    candidates = []
    if env.get('HUB_HOST_RELEASE'):
        candidates.append(Path(env['HUB_HOST_RELEASE']))
    argv0 = archive if archive is not None else (sys.argv[0] if sys.argv else '')
    if argv0:
        candidates.append(Path(argv0).resolve().parent)
    local = env.get('LOCALAPPDATA')
    data = Path(local) if local and os.name == 'nt' else Path.home() / '.local/share'
    candidates.append(data / 'agent-hub2' / 'current')
    candidates.append(data / 'agent-hub2' / 'venv')
    for release in candidates:
        found = _python_in(release)
        if found is not None:
            return found
    return None


def main(args):
    python = daemon_python()
    if python is None:
        print('hub host: no installed host release found; install it with '
              'https://hub.dtcdev.click/cli/host-install.sh (or host-install.ps1).', file=sys.stderr)
        return 1
    command = [str(python), *DAEMON_ARGS, *args]
    if os.name == 'nt':
        return subprocess.run(command).returncode
    os.execv(command[0], command)
