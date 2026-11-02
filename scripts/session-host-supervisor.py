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


def _bypass_proxy_for_hub():
    """Dead engine proxy env must not silence the supervisor or its children (2026-11-02)."""
    for name in ('NO_PROXY', 'no_proxy'):
        have = [h.strip() for h in os.environ.get(name, '').split(',') if h.strip()]
        os.environ[name] = ','.join(have + [h for h in ('hub.dtcdev.click', '.dtcdev.click') if h not in have])


_bypass_proxy_for_hub()


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
        error = RuntimeError('Native controller operation failed')
        # The reason (e.g. "a: no matching session") used to be discarded, which hid
        # why 238 controller restarts failed (2026-11-02); failure_reason logs it.
        error.exit_code = result.returncode
        error.stderr = (result.stderr or '').strip()[-300:]
        raise error
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
    env_args = [
        '--env', 'HUB_CONFIG_FILE=' + str(cred_path),
        '--env', 'PYTHONPATH=' + str(workspace / 'host'),
    ]
    for var in ('AGENT_HUB_MANAGED_DIR', 'CODEX_LAUNCH_WRAPPER', 'PATH',
                'CLAUDE_CODE_GIT_BASH_PATH', 'UV_PYTHON_INSTALL_DIR'):
        if var in os.environ:
            env_args.extend(['--env', f'{var}={os.environ[var]}'])
    attempt = lambda: run_controller(args, workspace, command, tag_prefix='hub-controller-',
                                     env_args=env_args, journal=journal, stop=stop)
    if args.control_dir and is_windows():
        # Windows Task Scheduler restarts a task only when it fails to LAUNCH,
        # never on a non-zero action exit, so one supervisor exception left
        # the host dead until next logon (intake 88ff6766). Retry in-process.
        from agent_hub_host.bootstrap_control import replace_pending
        supervise_windows(attempt, stop, replacing=lambda: replace_pending(args.control_dir))
    else:
        attempt()


# Planned exits the Windows controller.ps1 loop understands: 75 restarts the
# supervisor (Hub-pinned release), 76 ends the loop so the installer can
# replace the accepted handover's task.
RELEASE_RESTART_EXIT = 75
HANDOVER_EXIT = 76
RETRY_DELAYS_S = (5, 15, 30, 60)
HEALTHY_RUN_S = 300


def log_event(event, **fields):
    print(json.dumps({'event': event, **fields}), flush=True)


def failure_reason(error):
    """Bounded, credential-free reason for controller.log (type + short message)."""
    text = redact(str(error))[:240]
    reason = {'type': type(error).__name__, 'detail': text}
    if getattr(error, 'exit_code', None) is not None:
        reason['exit_code'] = error.exit_code
        reason['stderr'] = redact(str(getattr(error, 'stderr', '')))[:200]
    return reason


_SECRETISH = None


def redact(text):
    global _SECRETISH
    if _SECRETISH is None:
        import re
        _SECRETISH = re.compile(r'(?i)(bearer\s+\S+|(?:token|secret|password|key)["\']?\s*[:=]\s*\S+|[A-Za-z0-9_\-]{32,}(?:\.[A-Za-z0-9_\-]{8,}){0,2})')
    return _SECRETISH.sub('[redacted]', text)


def child_output_tail(receipt, limit=1500):
    """Best-effort tail of the dead child's own output, which never reaches controller.log."""
    try:
        ensure_runtime_bin_on_path()
        result = subprocess.run(['aplexer', 'capture', receipt['id'], '--plain'], capture_output=True,
                                text=True, timeout=15, stdin=subprocess.DEVNULL)
        if result.returncode:
            return None
        return redact(result.stdout[-limit:])
    except Exception:
        return None


def supervise_windows(attempt, stop, *, clock=None, replacing=lambda: False):
    """Run attempts until a signal or planned exit; failures are logged and retried."""
    import time
    clock = clock or time.monotonic
    failures = 0
    while not stop.is_set():
        if replacing():
            # The installer is replacing the task while no lease is held.
            log_event('controller_replace_exit')
            raise SystemExit(HANDOVER_EXIT)
        started = clock()
        try:
            attempt()
            return
        except SystemExit:
            raise
        except Exception as error:
            if clock() - started >= HEALTHY_RUN_S:
                failures = 0
            delay = RETRY_DELAYS_S[min(failures, len(RETRY_DELAYS_S) - 1)]
            failures += 1
            log_event('controller_retry', attempt=failures, retry_in_s=delay, **failure_reason(error))
            for _ in range(delay):
                if stop.wait(1) or replacing():
                    break


def confirm_owned_exit(receipt, *, polls=5, interval=2.0, sleep=None):
    """Positive Job Object exit proof for our exact child, killing it only if needed."""
    import time
    sleep = sleep or time.sleep
    row = native('status', receipt['id'])
    if not same_controller(row, receipt):
        raise RuntimeError('Controller identity changed; exit is unproven')
    if row.get('worker_alive') is not False or row.get('containment_empty') is not True:
        try:
            result = native('kill', receipt['id'], '--signal', 'TERM', '--grace-ms', '30000')  # aplexer max 30000; > session_host DRAIN_TIMEOUT_S (4398ce2a)
            # Pinned aplexer 0.1.23 removes a finished record only after
            # containment cleanup. An affirmative kill reply is proof.
            if result.get('id') == receipt['id'] and result.get('record_removed') is True:
                return
        except RuntimeError:
            # An already-exited child can refuse kill; status below decides.
            pass
    for index in range(polls):
        row = native('status', receipt['id'])
        if not same_controller(row, receipt):
            raise RuntimeError('Controller identity changed; exit is unproven')
        if row.get('containment_empty') is True:
            return
        if index + 1 < polls:
            sleep(interval)
    raise RuntimeError('Owned controller exit is unproven')


# A wedged controller (its aplexer worker stops answering RPCs, journal files
# freeze) used to make `aplexer status` time out forever: the supervisor then
# respawned a controller every ~20 s, each hung on the held journal lock (Tabo,
# 2026-11-02: 22 respawns). After this many consecutive status failures the
# supervisor kills the exact controller PIDs it last saw, forgets the record and
# retries, and it never starts a second controller while the journal lock is held.
STATUS_FAILURES_BEFORE_KILL = 3


def journal_lock_held(journal):
    """True when another process holds the controller's exclusive journal lock."""
    lock_path = pathlib.Path(journal) / 'host.lock'
    if not lock_path.exists():
        return False
    try:
        with lock_path.open('a') as lock:
            if os.name == 'nt':
                import msvcrt
                if os.fstat(lock.fileno()).st_size == 0:
                    return False
                os.lseek(lock.fileno(), 0, os.SEEK_SET)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    return True
                os.lseek(lock.fileno(), 0, os.SEEK_SET)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                return False
            import fcntl
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(lock, fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def kill_pid(pid):
    """Force-kill one PID and its tree; the PIDs come only from our own receipt's status rows."""
    if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
        return False
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True, timeout=30,
                           stdin=subprocess.DEVNULL)
        else:
            os.kill(pid, signal.SIGKILL)
        return True
    except Exception:
        return False


def recover_wedged_controller(receipt, pids, *, kill=None, forget=None):
    """Kill the wedged controller by PID and drop its aplexer record (aplexer forget --force)."""
    kill = kill or kill_pid
    forget = forget or (lambda rid: native('forget', rid, '--force'))
    killed = [pid for pid in pids if kill(pid)]
    try:
        forget(receipt['id'])
        forgotten = True
    except Exception:
        forgotten = False
    log_event('controller_wedge_recovered', id=receipt['id'], killed_pids=killed, forgotten=forgotten)
    return killed, forgotten


def start_child(workspace, command, tag, env_args):
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
    return receipt


def run_controller(args, workspace, command, *, tag_prefix, env_args, journal, stop):
    tag = tag_prefix + uuid.uuid4().hex[:12]
    with ExitStack() as stack:
        lease = None
        if args.control_dir:
            if not is_windows():
                raise ValueError('Installer handover is supported only for Windows Job Objects')
            from agent_hub_host.bootstrap_control import ControllerLease
            # NeedsAttention propagates: the Windows loop logs it and retries
            # recovery later; it never starts a duplicate child meanwhile.
            lease = stack.enter_context(ControllerLease(args.control_dir))
            from agent_hub_host.bootstrap_control import replace_pending
            if replace_pending(args.control_dir):
                log_event('controller_replace_exit')
                raise SystemExit(HANDOVER_EXIT)
        if journal_lock_held(journal):
            log_event('controller_spawn_blocked', reason='journal lock held by another controller')
            raise RuntimeError('Journal lock held by another controller; not starting a second one')
        try:
            receipt = start_child(workspace, command, tag, env_args)
        except BaseException:
            # aplexer may have created the child before a timeout or a bad
            # receipt; record the unique tag so no duplicate starts blindly.
            if lease:
                lease.publish_pending(tag)
            raise
        handing_over = False
        child_died = False
        wedge_recovered = False
        status_failures = 0
        last_pids = []
        try:
            if lease:
                lease.publish(receipt)
            log_event('controller_started', id=receipt['id'])
            while not stop.wait(5):
                if lease and lease.stop_requested():
                    handing_over = True
                    break
                try:
                    row = native('status', receipt['id'])
                except (RuntimeError, OSError, subprocess.TimeoutExpired):
                    status_failures += 1
                    if status_failures < STATUS_FAILURES_BEFORE_KILL:
                        continue
                    _killed, forgotten = recover_wedged_controller(receipt, last_pids)
                    # Exit is proven only when the record was actually forgotten; otherwise the normal
                    # cleanup path decides (and marks the lease unproven so the loop retries).
                    wedge_recovered = forgotten
                    raise RuntimeError('Controller wedged: status failed %d times; killed and forgotten' % status_failures)
                status_failures = 0
                if not same_controller(row, receipt):
                    raise RuntimeError('Controller identity changed')
                last_pids = [p for p in (row.get('worker_pid'), row.get('workload_pid')) if isinstance(p, int)]
                if row.get('phase') != 'running' or row.get('worker_alive') is False:
                    child_died = True
                    marker = journal / 'host-release-restart'
                    if marker.exists():
                        # Planned exit to load a Hub-pinned host release (intake
                        # a3227276): exit so the service restarts this supervisor
                        # on the new code too; not a crash.
                        marker.unlink()
                        log_event('controller_release_restart', id=receipt['id'])
                        raise SystemExit(RELEASE_RESTART_EXIT)
                    if not stop.is_set():
                        publish_controller_crash(row, receipt, journal / 'host-health.json')
                    raise RuntimeError('Controller exited; supervisor will restart')
        except BaseException as error:
            if not isinstance(error, SystemExit):
                log_event('controller_failed', id=receipt['id'], **failure_reason(error))
                if child_died:
                    tail = child_output_tail(receipt)
                    if tail:
                        log_event('controller_child_output', id=receipt['id'], tail=tail)
            raise
        finally:
            # Only our exact receipt qualifies for stop. Missing records are not
            # exit proof, and never cause another tag or PID to be selected.
            try:
                if wedge_recovered:
                    # Exit was forced by PID and the record forgotten, so a status
                    # query has nothing left to prove; do not mark the lease unproven.
                    if lease:
                        lease.confirm_exit()
                elif lease:
                    confirm_owned_exit(receipt)
                    lease.confirm_exit()
                else:
                    row = native('status', receipt['id'])
                    if same_controller(row, receipt):
                        native('kill', receipt['id'], '--signal', 'TERM', '--grace-ms', '30000')
            except Exception as cleanup_error:
                if lease:
                    lease.mark_unproven()
                log_event('controller_cleanup_unconfirmed', id=receipt['id'], **failure_reason(cleanup_error))
        if handing_over:
            # The installer accepted the handover and replaces the task; the
            # controller.ps1 loop ends on this code instead of restarting.
            raise SystemExit(HANDOVER_EXIT)

if __name__ == '__main__':
    main()
