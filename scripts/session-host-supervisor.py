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
from contextlib import ExitStack


def ensure_runtime_bin_on_path():
    """The logon task may lack the installer PATH; find <root>/bin next to venv/Scripts."""
    if sys.platform != 'win32':
        return
    candidate = pathlib.Path(sys.executable).resolve().parent.parent.parent / 'bin'
    if (candidate / 'aplexer.exe').is_file():
        parts = os.environ.get('PATH', '').split(os.pathsep)
        if str(candidate) not in parts:
            os.environ['PATH'] = os.pathsep.join([str(candidate), *parts])


def native(*argv):
    ensure_runtime_bin_on_path()
    command = ['aplexer', *argv]
    command.insert(command.index('--') if '--' in command else len(command), '--json')
    result = subprocess.run(command, capture_output=True, text=True, timeout=35)
    if result.returncode:
        raise RuntimeError('Native controller operation failed')
    if len(result.stdout) > 131072:
        raise RuntimeError('Native response exceeded capacity')
    return json.loads(result.stdout)


def is_windows() -> bool:
    """Windows session hosts (Tabo) use Job Objects, never Linux cgroup flags."""
    try:
        if os.name == "nt" or sys.platform == "win32":
            return True
    except Exception:
        pass
    try:
        import platform as _platform
        return _platform.system() == "Windows"
    except Exception:
        return False


def controller_resource_argv() -> list[str]:
    """Linux cgroup caps for the controller; empty on Windows (Job Objects)."""
    if is_windows():
        return []
    return ['--memory', '1500M', '--pids', '256',
            '--cpu-quota-us', '100000', '--cpu-period-us', '100000']


# aplexer 0.1.23 reports `workspace` as an observed, mutable field (on Windows it
# moved to %TEMP% seconds after session_host started), so it is not identity.
# The immutable receipt is id + created_at_ms + tag + engine + command. Matching
# on workspace made a healthy controller look "changed": the supervisor exited
# without killing it, the orphan kept the journal lock, every restart died on
# PermissionError and the lease stayed controller_exit_unproven (intake bf0863c4).
CONTROLLER_MATCH_KEYS = ('id', 'tag', 'engine', 'created_at_ms', 'command')


def same_controller(row, receipt):
    return all(row.get(key) == receipt.get(key) for key in CONTROLLER_MATCH_KEYS)


def controller_crash_service(row, receipt):
    """A missing/changed receipt or observation timeout is never crash evidence."""
    if (not same_controller(row, receipt) or receipt.get('engine') != 'shell'
            or row.get('worker_alive') is not False
            or row.get('phase') not in ('exited', 'broken', 'failed', 'dead', 'stopped')):
        return None
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    return dict(service_id='hub-controller', worker='broken', worker_state='broken',
                observed_at=stamp, observation_time=stamp, status_file_age_s=0,
                reason='service_crash')


def publish_controller_crash(row, receipt, health_path, *, collector_factory=None, publish=None):
    """Report only this supervisor's exact dead child; never restart other workers."""
    service = controller_crash_service(row, receipt)
    if service is None:
        return False
    try:
        from agent_hub_host import config
        from agent_hub_host.host_health_monitor import HostHealthMonitor, post_host_health_observation
        cfg = config.load_host_config()
        if not cfg.get('machine_id') or not cfg.get('access_token') or not cfg.get('base_url'):
            return False
        factory = collector_factory or HostHealthMonitor
        result = factory(config_path=str(health_path), machine_id=cfg['machine_id']).step(submit=False)
        payload = result.get('payload')
        if not isinstance(payload, dict):
            return False
        # Collect the other configured services afresh; TS retains prior positive
        # failure events independently across healthy/unknown snapshot replacement.
        services = [s for s in payload.get('services', []) if s.get('service_id') != 'hub-controller']
        services.sort(key=lambda s: s.get('worker') != 'broken')
        payload['services'] = services[:15] + [service]
        (publish or post_host_health_observation)(payload, base_url=cfg['base_url'], token=cfg['access_token'])
        return True
    except Exception as error:
        print(json.dumps({'event': 'controller_health_unconfirmed', 'type': type(error).__name__}), flush=True)
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--config', default=None, help='Subscription configuration (default: ~/.config/agent-hub/subscriptions.json)')
    parser.add_argument('--credential-config', default=None, help='Host credentials (default: ~/.config/agent-hub/host.json)')
    parser.add_argument('--journal-dir', default=None, help='Journal directory')
    parser.add_argument('--control-dir', default=None, help='Private cooperative Windows installer handover')
    args = parser.parse_args()
    workspace = pathlib.Path(args.workspace).resolve(strict=True)
    if args.config:
        config_path = pathlib.Path(args.config).resolve(strict=True)
    else:
        sys.path.insert(0, str(workspace / 'host'))
        from agent_hub_host import fleet
        config_path = fleet.subscriptions_path().resolve(strict=True)

    if args.credential_config:
        cred_path = pathlib.Path(args.credential_config).resolve(strict=True)
    else:
        sys.path.insert(0, str(workspace / 'host'))
        from agent_hub_host import config as host_config
        cred_path = host_config.get_host_config_path().resolve(strict=True)

    if args.journal_dir:
        journal = pathlib.Path(args.journal_dir).resolve()
    else:
        journal = (pathlib.Path.home() / ".local" / "state" / "agent-hub" / "journal").resolve()
    journal.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Parent health exports use the same explicitly selected host credentials as
    # the child, never an ambient human/job profile. No credentials are logged.
    sys.path.insert(0, str(workspace / 'host'))
    os.environ['HUB_CONFIG_FILE'] = str(cred_path)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    command = [sys.executable, '-u', '-m', 'agent_hub_host.session_host',
               '--config', str(config_path),
               '--journal-dir', str(journal)]
    tag = 'hub-controller-' + uuid.uuid4().hex[:12]
    env_args = [
        '--env', 'HUB_CONFIG_FILE=' + str(cred_path),
        '--env', 'PYTHONPATH=' + str(workspace / 'host'),
    ]
    for var in ('AGENT_HUB_MANAGED_DIR', 'CODEX_LAUNCH_WRAPPER', 'PATH',
                'CLAUDE_CODE_GIT_BASH_PATH', 'UV_PYTHON_INSTALL_DIR'):
        if var in os.environ:
            env_args.extend(['--env', f'{var}={os.environ[var]}'])
    with ExitStack() as stack:
        lease = None
        if args.control_dir:
            if not is_windows():
                raise ValueError('Installer handover is supported only for Windows Job Objects')
            from agent_hub_host.bootstrap_control import ControllerLease, NeedsAttention
            try:
                lease = stack.enter_context(ControllerLease(args.control_dir))
            except NeedsAttention:
                # Success exit stops the task's automatic failure/retry loop.
                # The durable status and log give an actionable recovery path.
                print(json.dumps({'event': 'needs_attention', 'reason': 'controller_exit_unproven',
                      'status_file': str(pathlib.Path(args.control_dir) / 'needs-attention.json')}), flush=True)
                return
        receipt = native('start', '--workspace', str(workspace), '--cwd', str(workspace),
                         '--tag', tag, '--engine', 'shell',
                         *env_args,
                         *controller_resource_argv(),
                         '--history-bytes', '262144',
                         '--', *command)
        # Windows hosts contain the controller with a Job Object (containment_empty);
        # Linux hosts use cgroup caps above. Exit proof never reads /sys/fs/cgroup on
        # Windows: native_sessions._confirm_workload_exit accepts catalog
        # containment_empty plus dead PIDs instead.
        # Reuse the receipt only in this process; no user-provided ownership labels
        # or persisted IDs can select another session for cleanup.
        if (receipt.get('engine') != 'shell' or receipt.get('command') != command or receipt.get('workspace') != str(workspace)
                or receipt.get('tag') != tag or not receipt.get('id') or type(receipt.get('created_at_ms')) is not int):
            raise RuntimeError('Native controller receipt mismatch')
        handing_over = False
        try:
            if lease:
                lease.publish(receipt)
            print(json.dumps({'event': 'controller_started', 'id': receipt['id']}), flush=True)
            while not stop.wait(5):
                if lease and lease.stop_requested():
                    handing_over = True
                    break
                row = native('status', receipt['id'])
                if not same_controller(row, receipt):
                    raise RuntimeError('Controller identity changed')
                if row.get('phase') != 'running' or row.get('worker_alive') is False:
                    marker = journal / 'host-release-restart'
                    if marker.exists():
                        # Planned exit to load a Hub-pinned host release (intake
                        # a3227276): exit so the service restarts this supervisor
                        # on the new code too; not a crash.
                        marker.unlink()
                        print(json.dumps({'event': 'controller_release_restart', 'id': receipt['id']}), flush=True)
                        raise SystemExit(75)
                    if not stop.is_set():
                        publish_controller_crash(row, receipt, journal / 'host-health.json')
                    raise RuntimeError('Controller exited; supervisor will restart')
        finally:
            # Only our exact receipt qualifies for stop. Missing records are not
            # exit proof, and never cause another tag or PID to be selected.
            try:
                row = native('status', receipt['id'])
                if same_controller(row, receipt):
                    result = native('kill', receipt['id'], '--signal', 'TERM', '--grace-ms', '30000')  # aplexer max 30000; > session_host DRAIN_TIMEOUT_S (4398ce2a)
                    if lease:
                        # Pinned aplexer 0.1.23 removes a finished record only
                        # after containment cleanup. An affirmative kill reply
                        # is proof; an independently missing record is not.
                        proven = result.get('id') == receipt['id'] and result.get('record_removed') is True
                        if not proven:
                            row = native('status', receipt['id'])
                            proven = same_controller(row, receipt) and row.get('containment_empty') is True
                        if not proven:
                            raise RuntimeError('Owned controller exit is unproven')
                        lease.confirm_exit()
                elif lease:
                    raise RuntimeError('Controller identity changed; exit is unproven')
            except Exception:
                if lease:
                    lease.mark_unproven()
                print(json.dumps({'event': 'controller_cleanup_unconfirmed', 'id': receipt['id']}), flush=True)
        if handing_over:
            # Restart-on-failure restores the old runner if the installer dies
            # after shutdown was accepted. A successful installer replaces and
            # starts the task immediately; the lease also excludes duplicates.
            raise SystemExit(75)

if __name__ == '__main__':
    main()
