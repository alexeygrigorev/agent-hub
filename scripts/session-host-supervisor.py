#!/usr/bin/env python3
"""Supervise one genuinely bound native Hub host, never another session's process."""
import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import threading
import uuid


def native(*argv):
    command = ['aplexer', *argv]
    command.insert(command.index('--') if '--' in command else len(command), '--json')
    result = subprocess.run(command, capture_output=True, text=True, timeout=35)
    if result.returncode:
        raise RuntimeError('Native controller operation failed')
    if len(result.stdout) > 131072:
        raise RuntimeError('Native response exceeded capacity')
    return json.loads(result.stdout)


def same_controller(row, receipt):
    return all(row.get(key) == receipt.get(key) for key in
               ('id', 'tag', 'workspace', 'engine', 'created_at_ms', 'command'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--credential-config', required=True)
    parser.add_argument('--journal-dir', required=True)
    args = parser.parse_args()
    workspace = pathlib.Path(args.workspace).resolve(strict=True)
    journal = pathlib.Path(args.journal_dir).resolve()
    journal.mkdir(parents=True, exist_ok=True, mode=0o700)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    command = [sys.executable, '-u', '-m', 'agent_hub_host.session_host',
               '--config', str(pathlib.Path(args.config).resolve(strict=True)),
               '--journal-dir', str(journal)]
    tag = 'hub-controller-' + uuid.uuid4().hex[:12]
    env_args = [
        '--env', 'HUB_CONFIG_FILE=' + str(pathlib.Path(args.credential_config).resolve(strict=True)),
        '--env', 'PYTHONPATH=' + str(workspace / 'host'),
    ]
    for var in ('AGENT_HUB_MANAGED_DIR', 'HUB_MANAGED_DIR', 'CODEX_LAUNCH_WRAPPER'):
        if var in os.environ:
            env_args.extend(['--env', f'{var}={os.environ[var]}'])
    receipt = native('start', '--workspace', str(workspace), '--cwd', str(workspace),
                     '--tag', tag, '--engine', 'shell',
                     *env_args,
                     '--memory', '1500M', '--pids', '256', '--history-bytes', '262144',
                     '--', *command)
    # Reuse the receipt only in this process; no user-provided ownership labels
    # or persisted IDs can select another session for cleanup.
    if (receipt.get('engine') != 'shell' or receipt.get('command') != command or receipt.get('workspace') != str(workspace)
            or receipt.get('tag') != tag or not receipt.get('id') or type(receipt.get('created_at_ms')) is not int):
        raise RuntimeError('Native controller receipt mismatch')
    print(json.dumps({'event': 'controller_started', 'id': receipt['id']}), flush=True)
    try:
        while not stop.wait(5):
            row = native('status', receipt['id'])
            if not same_controller(row, receipt):
                raise RuntimeError('Controller identity changed')
            if row.get('phase') != 'running' or row.get('worker_alive') is False:
                raise RuntimeError('Controller exited; supervisor will restart')
    finally:
        # Only our exact receipt qualifies for stop. Missing records are not
        # exit proof, and never cause another tag or PID to be selected.
        try:
            row = native('status', receipt['id'])
            if same_controller(row, receipt):
                native('kill', receipt['id'], '--signal', 'TERM', '--grace-ms', '10000')
        except Exception:
            print(json.dumps({'event': 'controller_cleanup_unconfirmed', 'id': receipt['id']}), flush=True)

if __name__ == '__main__':
    main()
