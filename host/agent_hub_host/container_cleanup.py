"""RMTHZ host hook: only remove never-started agent containers after Hub stop.

Run periodically as the host operator with Docker access and Hub credentials.
Default is dry-run; no Docker force/volume removal is used. Lookup failures
and unknown session states retain containers. Logs contain only IDs/actions.
"""
import argparse
import json
import re
import subprocess

SESSION = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}\Z')


def eligible(container, session_id):
    labels = container.get('Config', {}).get('Labels') or {}
    state = container.get('State') or {}
    return (bool(SESSION.fullmatch(session_id))
            and labels.get('agent.session') == session_id
            and not any(k.lower().startswith(('coolify', 'com.coolify')) for k in labels)
            and state.get('Status') == 'created'
            and state.get('StartedAt', '').startswith('0001-01-01T00:00:00'))


def command(argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True,
                          timeout=30).stdout


def sweep(apply=False, hub_bin='hub', run=command):
    ids = run(['docker', 'ps', '-aq', '--filter', 'status=created',
               '--filter', 'label=agent.session']).split()
    for cid in ids:
        if not re.fullmatch(r'[0-9a-f]{12,64}', cid):
            continue
        try:
            container = json.loads(run(['docker', 'inspect', cid]))[0]
            sid = (container.get('Config', {}).get('Labels') or {}).get('agent.session', '')
            if not eligible(container, sid):
                continue
            session = json.loads(run([hub_bin, 'sessions', 'show', sid]))
            # 'failed' may still carry an uncertain native process. Only the
            # Hub's confirmed stopped state authorizes this host cleanup.
            if session.get('id') != sid or session.get('state') != 'stopped':
                continue
            print(f'dry-run container={cid} session={sid} action=remove-created', flush=True)
            if apply:
                current = json.loads(run(['docker', 'inspect', cid]))[0]
                if eligible(current, sid):
                    run(['docker', 'rm', cid])
                    print(f'removed container={cid} session={sid}', flush=True)
        except (subprocess.SubprocessError, OSError, ValueError, KeyError, IndexError, TypeError):
            print(f'retained container={cid} reason=lookup-or-removal-failed', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--hub-bin', default='hub')
    args = parser.parse_args()
    sweep(args.apply, args.hub_bin)


if __name__ == '__main__':
    main()
