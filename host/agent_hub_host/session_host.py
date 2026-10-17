"""Foreground Fleet/command host; runs only inside its real native controller session."""
import argparse
import fcntl
import json
import signal
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from . import config, fleet, session_control
from .native_sessions import qualify_sender_binding
from .quota_snapshot import QuotaCollector
from .status_runtime import StatusCollector


def status_step_once(collector) -> None:
    """Run ONE isolated status round (call inside the observer thread only).

    Bounded count/error log only; rate-limited rounds stay silent (no log
    flood); missing allowlist yields an error count with no HTTP. Never
    raises: every failure is recorded as a bounded error line. Never touches
    quota or command state.
    """
    try:
        result = collector.step(submit=True)
    except Exception as error:
        print(json.dumps({'event': 'status_error', 'type': type(error).__name__}), flush=True)
        return
    try:
        if not isinstance(result, dict) or result.get('rate_limited'):
            return
        observations = result.get('observations')
        skipped = result.get('skipped')
        errors = result.get('errors')
        print(json.dumps({'event': 'status_cycle',
                          'observations': len(observations) if isinstance(observations, list) else 0,
                          'skipped': len(skipped) if isinstance(skipped, list) else 0,
                          'errors': len(errors) if isinstance(errors, list) else 0}), flush=True)
    except Exception as error:
        print(json.dumps({'event': 'status_error', 'type': type(error).__name__}), flush=True)


def maybe_start_status_thread(current: Optional[threading.Thread], collector) -> Optional[threading.Thread]:
    """Start ONE bounded daemon observer thread, or skip while one is alive.

    Single-inflight inside the existing controller: no new daemon, timer, or
    controller. A blocked collector (long probes/HTTP) never delays the
    command poll loop; the next round is skipped until the thread exits. The
    collector's own 60s cadence rate-limits real work. Daemon threads die
    with the controller process (native cgroup lifecycle).
    """
    try:
        if current is not None and current.is_alive():
            return current
        thread = threading.Thread(target=status_step_once, args=(collector,),
                                  daemon=True, name='status-observe')
        thread.start()
        return thread
    except Exception as error:
        print(json.dumps({'event': 'status_error', 'type': type(error).__name__}), flush=True)
        return current


def run(settings_path, journal_dir, *, interval=2, fleet_interval=20, cycles=None):
    if interval < 1 or fleet_interval < interval:
        raise ValueError('Invalid host polling intervals')
    cfg = config.load_config()
    if not cfg.get('machine_id') or not cfg.get('access_token'):
        raise ValueError('An enrolled private host configuration is required')
    journal = Path(journal_dir).resolve()
    journal.mkdir(parents=True, exist_ok=True, mode=0o700)
    # One controller per journal; a supervisor restart cannot duplicate effects.
    with (journal / 'host.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        settings = fleet.load_settings(settings_path)
        native = settings.get('aplexer_bin', 'aplexer')
        binding = qualify_sender_binding(native, str(Path.cwd().resolve()))
        # M15d narrow plumbing: surface the trusted cleanup-root allowlist
        # size (no auto-delete daemon here; tree removal is explicit CLI only).
        roots = settings.get('cleanup_roots', [])
        print(json.dumps({'event': 'host_ready', 'native_controller': binding.session_id,
                          'cleanup_roots': len(roots) if isinstance(roots, list) else 0}), flush=True)
        stop = threading.Event()
        previous = {}
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
        dispatcher = fleet.Dispatcher(settings)
        quota_collector = QuotaCollector(base_url=cfg.get('base_url'))
        # Status observation hook (released StatusCollector source): ONE
        # instance for the controller lifetime, trusted allowlist path under
        # the existing journal, this machine, 60s floor. The loop below only
        # starts/skips the single-inflight daemon thread; the collector owns
        # cadence, and quota/command cycles never wait for it.
        status_collector = StatusCollector(
            allowlist_path=str(journal / 'status-allowlist.json'),
            machine_id=cfg['machine_id'], min_interval_s=60)
        status_thread: Optional[threading.Thread] = None
        next_fleet = 0
        count = 0
        try:
            while not stop.is_set() and (cycles is None or count < cycles):
                if time.monotonic() >= next_fleet:
                    try:
                        results = dispatcher.once()
                        print(json.dumps({'event': 'fleet_cycle', 'results': len(results)}), flush=True)
                    except Exception as error:
                        print(json.dumps({'event': 'fleet_error', 'type': type(error).__name__}), flush=True)
                    next_fleet = time.monotonic() + fleet_interval
                # A failed subscription/placement cycle must not prevent stop,
                # capture, or late delivery reconciliation on existing sessions.
                try:
                    session_control.execute(SimpleNamespace(
                        sessions_cmd='host-receive', once=True, machine_id=cfg['machine_id'],
                        actor=None, credential_family=None, journal_dir=str(journal),
                        aplexer_bin=native, limit=20, sender_workspace=binding.workspace,
                        # M15d narrow plumbing: HOST-config trusted cleanup roots
                        # arm the STOP-bundled owning-host tree step (never CLI
                        # paths). Invalid shapes fail that tree to uncertain;
                        # stops are unaffected.
                        cleanup_roots=settings.get('cleanup_roots', []), settings=None))
                except Exception as error:
                    print(json.dumps({'event': 'commands_error', 'type': type(error).__name__}), flush=True)
                try:
                    before = quota_collector.last_collect_monotonic
                    snapshot = quota_collector.step()
                    if before != quota_collector.last_collect_monotonic:
                        print(json.dumps({'event': 'quota_cycle', 'reported': snapshot is not None,
                                          'held': bool(quota_collector.last_hold),
                                          'error': bool(quota_collector.last_error)}), flush=True)
                except Exception as error:
                    print(json.dumps({'event': 'quota_error', 'type': type(error).__name__}), flush=True)
                # Status observation never blocks commands/quota: one bounded
                # single-inflight daemon thread, skipped while alive.
                status_thread = maybe_start_status_thread(status_thread, status_collector)
                count += 1
                if cycles is None or count < cycles:
                    stop.wait(interval)
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--journal-dir', required=True)
    parser.add_argument('--interval', type=float, default=2)
    parser.add_argument('--fleet-interval', type=float, default=20)
    args = parser.parse_args()
    run(args.config, args.journal_dir, interval=args.interval, fleet_interval=args.fleet_interval)

if __name__ == '__main__':
    main()
