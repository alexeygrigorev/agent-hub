"""`hub` wheel console entry: the promised host namespace, not a relabeled daemon parser.

A wheel-installed `hub` is not the operator zipapp. It exposes only the
`hub host ...` namespace through the same seam the operator CLI uses: it
re-executes this release interpreter running the daemon module in isolated
mode (`-I -m agent_hub_host.cli`). Direct `python -m agent_hub_host.cli`
invocation remains the internal daemon surface.
"""
import os
import subprocess
import sys

DAEMON_ARGS = ('-I', '-m', 'agent_hub_host.cli')


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] in (['--help'], ['-h']):
        print('hub: wheel console entry. Only the host namespace is provided here:\n'
              '  hub host COMMAND ...   host daemon administration (try: hub host --help)\n'
              'The full operator CLI (sessions, intake, auth login, ...) is the hub.pyz\n'
              'zipapp installed by host-install.sh / host-install.ps1.')
        return 0
    if args[:1] != ['host']:
        print('hub: unsupported command for this wheel entry; only "hub host ..." is '
              'available. Install the operator CLI (hub.pyz) for the full CLI.',
              file=sys.stderr)
        return 2
    command = [sys.executable, *DAEMON_ARGS, *args[1:]]
    if os.name == 'nt':
        return subprocess.run(command).returncode
    os.execv(command[0], command)
