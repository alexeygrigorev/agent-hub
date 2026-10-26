"""Foreground Fleet/command host; runs only inside its real native controller session."""
import argparse
try:
    import fcntl
except ImportError:
    fcntl = None
import json
import signal
import threading
import time
import pathlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from . import auth, config, fleet, session_control
from .dispatch_diagnostics import MESSAGES as DISPATCH_MESSAGES
from .disk_monitor import DiskMonitor
from .host_health_monitor import HostHealthMonitor
from .jobs import JobDispatcher
from .metrics_producer import MetricsProducer
from .native_sessions import qualify_sender_binding
from .quota_snapshot import QuotaCollector
from .token_usage import TokenCollector
from .role_dispatch import TrustedRoleConfig, load_role_config
from .runtime import RuntimeManager
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


def metrics_step_once(collector) -> None:
    """Run ONE isolated metrics production round (observer thread only).

    Bounded failure-class log only; rate-limited/disabled rounds stay silent.
    Missing config means disabled (no exporter, no HTTP). Never raises, never
    touches quota/command state, never logs config/token/stderr/report data.
    """
    try:
        result = collector.step(submit=True)
    except Exception as error:
        print(json.dumps({'event': 'metrics_error', 'type': type(error).__name__}), flush=True)
        return
    try:
        if not isinstance(result, dict) or result.get('rate_limited') or result.get('disabled'):
            return
        errors = result.get('errors')
        print(json.dumps({'event': 'metrics_cycle',
                          'posted': bool(result.get('posted')),
                          'errors': len(errors) if isinstance(errors, list) else 0}), flush=True)
    except Exception as error:
        print(json.dumps({'event': 'metrics_error', 'type': type(error).__name__}), flush=True)


def maybe_start_metrics_thread(current: Optional[threading.Thread], collector) -> Optional[threading.Thread]:
    """Start ONE bounded metrics observer thread, or skip while one is alive.

    Same single-inflight contract as status: a blocked exporter/HTTP never
    delays commands/quota; the collector's own >=60s cadence (default 5min)
    rate-limits real work. No extra timer/service/dispatcher.
    """
    try:
        if current is not None and current.is_alive():
            return current
        thread = threading.Thread(target=metrics_step_once, args=(collector,),
                                  daemon=True, name='metrics-produce')
        thread.start()
        return thread
    except Exception as error:
        print(json.dumps({'event': 'metrics_error', 'type': type(error).__name__}), flush=True)
        return current


def token_step_once(collector) -> None:
    """Run ONE isolated native token collection round (observer thread only).

    Bounded count/error log only; rate-limited rounds stay silent. Never
    raises, never touches quota/command state, never logs token counts beyond
    the posted hour count.
    """
    try:
        result = collector.step()
    except Exception as error:
        print(json.dumps({'event': 'token_error', 'type': type(error).__name__}), flush=True)
        return
    try:
        if not isinstance(result, dict):
            return
        print(json.dumps({'event': 'token_cycle',
                          'hours': result.get('hours', 0),
                          'held': bool(collector.last_hold),
                          'error': bool(collector.last_error)}), flush=True)
    except Exception as error:
        print(json.dumps({'event': 'token_error', 'type': type(error).__name__}), flush=True)


def maybe_start_token_thread(current: Optional[threading.Thread], collector) -> Optional[threading.Thread]:
    """Start ONE bounded token observer thread, or skip while one is alive.

    Same single-inflight contract as status/metrics/disk: a blocked scan/HTTP
    never delays commands/quota; the collector's own hourly cadence
    rate-limits real work. No extra timer/service/dispatcher.
    """
    try:
        if current is not None and current.is_alive():
            return current
        thread = threading.Thread(target=token_step_once, args=(collector,),
                                  daemon=True, name='token-collect')
        thread.start()
        return thread
    except Exception as error:
        print(json.dumps({'event': 'token_error', 'type': type(error).__name__}), flush=True)
        return current


def disk_step_once(collector) -> None:
    """Run ONE isolated disk monitoring round (observer thread only).

    Bounded count/error log only; rate-limited/disabled rounds stay silent.
    Missing config means disabled (no probe, no HTTP). Never raises, never
    touches quota or command state.
    """
    try:
        result = collector.step(submit=True)
    except Exception as error:
        print(json.dumps({'event': 'disk_error', 'type': type(error).__name__}), flush=True)
        return
    try:
        if not isinstance(result, dict) or result.get('rate_limited') or result.get('disabled'):
            return
        errors = result.get('errors')
        filesystems = result.get('filesystems')
        print(json.dumps({'event': 'disk_cycle',
                          'posted': bool(result.get('posted')),
                          'filesystems': len(filesystems) if isinstance(filesystems, list) else 0,
                          'errors': len(errors) if isinstance(errors, list) else 0}), flush=True)
    except Exception as error:
        print(json.dumps({'event': 'disk_error', 'type': type(error).__name__}), flush=True)


def maybe_start_disk_thread(current: Optional[threading.Thread], collector) -> Optional[threading.Thread]:
    """Start ONE bounded disk observer thread, or skip while one is alive.

    Same single-inflight contract as status and metrics: a blocked probe/HTTP
    never delays commands/quota; the collector's own cadence (default 60s)
    rate-limits real work. No extra timer/service/dispatcher.
    """
    try:
        if current is not None and current.is_alive():
            return current
        thread = threading.Thread(target=disk_step_once, args=(collector,),
                                  daemon=True, name='disk-monitor')
        thread.start()
        return thread
    except Exception as error:
        print(json.dumps({'event': 'disk_error', 'type': type(error).__name__}), flush=True)
        return current


def host_health_step_once(collector) -> None:
    """Run ONE isolated host health monitoring round (observer thread only).

    Bounded count/error log only; rate-limited/disabled rounds stay silent.
    Missing config uses default enabled status. Never raises, never
    touches quota or command state.
    """
    try:
        result = collector.step(submit=True)
    except Exception as error:
        print(json.dumps({'event': 'host_health_error', 'type': type(error).__name__}), flush=True)
        return
    try:
        if not isinstance(result, dict) or result.get('rate_limited') or result.get('disabled'):
            return
        errors = result.get('errors')
        print(json.dumps({'event': 'host_health_cycle',
                          'posted': bool(result.get('posted')),
                          'errors': len(errors) if isinstance(errors, list) else 0}), flush=True)
    except Exception as error:
        print(json.dumps({'event': 'host_health_error', 'type': type(error).__name__}), flush=True)


def maybe_start_host_health_thread(current: Optional[threading.Thread], collector) -> Optional[threading.Thread]:
    """Start ONE bounded host health observer thread, or skip while one is alive.

    Same single-inflight contract as disk, status and metrics: a blocked probe/HTTP
    never delays commands/quota; the collector's own cadence (default 60s)
    rate-limits real work. No extra timer/service/dispatcher.
    """
    try:
        if current is not None and current.is_alive():
            return current
        thread = threading.Thread(target=host_health_step_once, args=(collector,),
                                  daemon=True, name='host-health-monitor')
        thread.start()
        return thread
    except Exception as error:
        print(json.dumps({'event': 'host_health_error', 'type': type(error).__name__}), flush=True)
        return current


def get_cleanup_roots(settings: Optional[dict]) -> List[str]:
    """Expose safe callable config for the normalhost loop cleaner.

    Returns the validated cleanup_roots list from settings, or empty list if absent/invalid.
    """
    if not isinstance(settings, dict):
        return []
    roots = settings.get("cleanup_roots", [])
    if isinstance(roots, list):
        return [r for r in roots if isinstance(r, str) and r.startswith("/") and "\x00" not in r]
    return []


def fleet_settings_mtime(settings_path) -> Optional[float]:
    """Best-effort mtime of the operator fleet settings file (None when unreadable)."""
    if not settings_path:
        return None
    try:
        return Path(settings_path).stat().st_mtime
    except OSError:
        return None


def maybe_reload_fleet_settings(settings_path, last_mtime: Optional[float]):
    """Reload operator fleet settings when the file changed since last load.

    Returns (settings_or_None, mtime). None means "keep running with the
    current settings": unchanged file, missing file, or an invalid edit
    (invalid edits fail closed on the last good settings and are retried
    after the next edit; the attempt mtime is remembered so a broken file
    does not log on every loop iteration). Never raises.
    """
    current = fleet_settings_mtime(settings_path)
    if current is None or (last_mtime is not None and current == last_mtime):
        return None, last_mtime
    try:
        reloaded = fleet.load_settings(settings_path)
    except Exception as error:
        print(json.dumps({'event': 'settings_reload_error', 'type': type(error).__name__}), flush=True)
        return None, current
    if not isinstance(reloaded, dict):
        print(json.dumps({'event': 'settings_reload_error', 'type': 'invalid_settings'}), flush=True)
        return None, current
    return reloaded, current


class RoleConsumer:
    """Explicitly enabled role job consumer hook for session host.

    Disabled by default. Single-inflight, never blocks command/quota/fleet loops.
    No duplicate persistent daemon and no canonical root state takeover.
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        role_config: Optional[TrustedRoleConfig] = None,
        base_url: Optional[str] = None,
        host_id: Optional[str] = None,
        token_provider: Optional[Callable[[], str]] = None,
        dispatcher: Optional[JobDispatcher] = None,
        min_interval_s: float = 10.0,
        enabled: bool = False,
    ):
        self.config_path = config_path
        self.role_config = role_config
        self.base_url = base_url
        self.host_id = host_id
        self.token_provider = token_provider or auth.get_access_token
        self.dispatcher = dispatcher
        self.min_interval_s = max(1.0, float(min_interval_s))
        self.last_run_monotonic = 0.0
        self.enabled = bool(enabled)
        self._last_config_mtime: Optional[float] = None

    def step(self) -> Dict[str, Any]:
        now = time.monotonic()
        if now - self.last_run_monotonic < self.min_interval_s:
            return {"rate_limited": True}
        self.last_run_monotonic = now

        if not self.enabled:
            return {"disabled": True, "reason": "role_consumer_disabled"}

        # Detect on-disk trusted config changes and fence active dispatcher
        if self.config_path:
            p = pathlib.Path(self.config_path)
            if p.is_file():
                try:
                    cur_mtime = p.stat().st_mtime
                    if self._last_config_mtime is None or cur_mtime != self._last_config_mtime:
                        new_cfg = load_role_config(self.config_path)
                        if new_cfg is not None and (self.role_config is None or new_cfg.config_hash != getattr(self.role_config, "config_hash", "")):
                            self.role_config = new_cfg
                            if self.dispatcher and hasattr(self.dispatcher, "runtime_mgr"):
                                self.dispatcher.runtime_mgr.role_config = self.role_config
                        self._last_config_mtime = cur_mtime
                except Exception:
                    pass

        cfg = self.role_config
        if cfg is None and self.config_path:
            cfg = load_role_config(self.config_path)
            self.role_config = cfg
        if cfg is None or not cfg.enabled:
            return {"disabled": True}

        if self.dispatcher is None:
            if not self.base_url or not self.host_id:
                return {"disabled": True, "reason": "missing_host_context"}
            mgr = RuntimeManager(role_config=cfg)
            self.dispatcher = JobDispatcher(
                base_url=self.base_url,
                host_id=self.host_id,
                token_provider=self.token_provider,
                runtime_mgr=mgr,
            )
        else:
            if hasattr(self.dispatcher, "runtime_mgr") and hasattr(self.dispatcher.runtime_mgr, "role_config"):
                self.dispatcher.runtime_mgr.role_config = cfg

        try:
            results = self.dispatcher.poll_and_dispatch_role_jobs()
            dispatched = sum(1 for r in results if isinstance(r, dict) and r.get("accepted"))
            recovered = sum(1 for r in results if isinstance(r, dict) and r.get("recovered"))
            errors = sum(1 for r in results if isinstance(r, dict) and not r.get("accepted") and not r.get("recovered"))
            return {
                "dispatched": dispatched,
                "recovered": recovered,
                "errors": errors,
                "idle": len(results) == 0,
            }
        except Exception as e:
            return {"error": type(e).__name__}


def role_consumer_step_once(consumer: RoleConsumer) -> None:
    """Run ONE isolated role consumption round (observer/worker thread only).

    Bounded failure log only. Never raises, never touches quota or session command state.
    """
    try:
        result = consumer.step()
    except Exception as error:
        print(json.dumps({"event": "role_consumer_error", "type": type(error).__name__}), flush=True)
        return
    try:
        if not isinstance(result, dict) or result.get("rate_limited") or result.get("disabled") or result.get("idle"):
            return
        if result.get("error"):
            print(json.dumps({"event": "role_consumer_error", "type": result["error"]}), flush=True)
            return
        print(json.dumps({
            "event": "role_cycle",
            "dispatched": result.get("dispatched", 0),
            "recovered": result.get("recovered", 0),
            "errors": result.get("errors", 0)
        }), flush=True)
    except Exception as error:
        print(json.dumps({"event": "role_consumer_error", "type": type(error).__name__}), flush=True)


def maybe_start_role_consumer_thread(current: Optional[threading.Thread], consumer: RoleConsumer) -> Optional[threading.Thread]:
    """Start ONE bounded role consumer thread, or skip while one is alive.

    Single-inflight inside existing controller: no new daemon, timer, or controller.
    A blocked dispatch never delays commands/quota/fleet.
    """
    try:
        if current is not None and current.is_alive():
            return current
        thread = threading.Thread(target=role_consumer_step_once, args=(consumer,),
                                  daemon=True, name="role-consumer")
        thread.start()
        return thread
    except Exception as error:
        print(json.dumps({"event": "role_consumer_error", "type": type(error).__name__}), flush=True)
        return current


# Must stay below the supervisor kill grace (scripts/session-host-supervisor.py).
DRAIN_TIMEOUT_S = 25.0


def maintenance_step_once(dispatcher, quota_collector):
    """Placement and quota can take minutes; never run them on the command loop."""
    started = time.monotonic()
    try:
        results = dispatcher.once()
        print(json.dumps({'event': 'fleet_cycle', 'results': len(results)}), flush=True)
        for result in results:
            if (isinstance(result, dict) and result.get('state') in {'failed', 'held'}
                    and result.get('reason') in DISPATCH_MESSAGES):
                # Whitelist diagnostics; no arbitrary exception text or payload.
                print(json.dumps({'event': 'fleet_dispatch_refused',
                                  'state': result['state'], 'reason': result['reason']}), flush=True)
    except Exception as error:
        print(json.dumps({'event': 'fleet_error', 'type': type(error).__name__}), flush=True)
    try:
        before = quota_collector.last_collect_monotonic
        snapshot = quota_collector.step()
        if before != quota_collector.last_collect_monotonic:
            print(json.dumps({'event': 'quota_cycle', 'reported': snapshot is not None,
                              'held': bool(quota_collector.last_hold),
                              'error': bool(quota_collector.last_error)}), flush=True)
    except Exception as error:
        print(json.dumps({'event': 'quota_error', 'type': type(error).__name__}), flush=True)
    print(json.dumps({'event': 'maintenance_cycle',
                      'elapsed_s': round(time.monotonic() - started, 3)}), flush=True)


def telemetry_step_once(dispatcher):
    """Fast heartbeat independent of the slow cycle (intake 4398ce2a)."""
    started = time.monotonic()
    try:
        hb = dispatcher.telemetry_tick()
        print(json.dumps({'event': 'telemetry_cycle', 'posted': True,
                          'launcher_resources': 'launcher_resources' in hb,
                          'gate': bool((hb.get('gate') or {}).get('launch_allowed')),
                          'elapsed_s': round(time.monotonic() - started, 3)}), flush=True)
    except Exception as error:
        print(json.dumps({'event': 'telemetry_error', 'type': type(error).__name__}), flush=True)


def offers_step_once(dispatcher):
    """Fast offer claiming independent of the slow cycle (intake 4398ce2a)."""
    try:
        out = dispatcher.offers_tick()
        if out.get('offers'):
            print(json.dumps({'event': 'offers_cycle', 'offers': out['offers'],
                              'dispatch_max_s': out['dispatch_max_s']}), flush=True)
        for result in out.get('results', []):
            if (isinstance(result, dict) and result.get('state') in {'failed', 'held'}
                    and result.get('reason') in DISPATCH_MESSAGES):
                print(json.dumps({'event': 'fleet_dispatch_refused',
                                  'state': result['state'], 'reason': result['reason']}), flush=True)
    except Exception as error:
        print(json.dumps({'event': 'offers_error', 'type': type(error).__name__}), flush=True)


def bindings_step_once(dispatcher, *, enrich=False):
    """Report native identity without waiting for launcher first-action readiness."""
    started = time.monotonic()
    try:
        report = (dispatcher.report_catalog_bindings if enrich else
                  getattr(dispatcher, 'report_native_bindings', dispatcher.report_catalog_bindings))
        report()
        print(json.dumps({'event': 'bindings_enrichment_cycle' if enrich else 'bindings_cycle',
                          'elapsed_s': round(time.monotonic() - started, 3)}), flush=True)
    except Exception as error:
        # Preserve dispatch and keep diagnostics bounded, including the common
        # client ValueError form of a telemetry 400. Never log response bodies.
        status = getattr(error, 'code', None)
        if type(status) is not int:
            status = 400 if 'status 400' in str(error) else None
        print(json.dumps({'event': 'bindings_enrichment_error' if enrich else 'bindings_error',
                          'type': type(error).__name__,
                          'http_status': status,
                          'elapsed_s': round(time.monotonic() - started, 3)}), flush=True)


def bindings_loop(dispatcher, stop, *, clock=time.monotonic):
    """One observer for in-flight starts and a bounded late-adoption tail.

    A launcher can await its child's first action for minutes. Its offer
    thread cannot report bindings during that wait, and the slow maintenance
    census is also minutes apart. Send a full native identity census while
    slow enrichment runs separately; this observer never claims or starts work.
    Idle controllers make no additional HTTP requests. A recovered journal
    receives the same short tail to adopt native starts after a restart.
    """
    records = getattr(dispatcher, '_active_launches', None)
    tail_until = clock() + 30 if isinstance(records, dict) and records else 0
    sampler = None
    try:
        while not stop.is_set():
            started = clock()
            inflight = getattr(dispatcher, '_inflight', 0)
            active = type(inflight) is int and inflight > 0
            if active:
                tail_until = clock() + 30
            if clock() < tail_until:
                bindings_step_once(dispatcher)
                if callable(getattr(dispatcher, 'report_native_bindings', None)):
                    # At most one slow sampler, independent of fast reporting.
                    # Its existing lock serializes durable progress and exit
                    # evidence with maintenance and post-dispatch sampling.
                    if sampler is None or not sampler.is_alive():
                        sampler = threading.Thread(
                            target=lambda: bindings_step_once(dispatcher, enrich=True),
                            daemon=True, name='fleet-binding-enrichment')
                        sampler.start()
                if active:
                    tail_until = clock() + 30
            stop.wait(max(0, 5 - (clock() - started)))
    finally:
        if sampler is not None:
            sampler.join(timeout=1)


def maybe_start_named_thread(current, target, args, name):
    if current is not None and current.is_alive():
        return current
    thread = threading.Thread(target=target, args=args, daemon=True, name=name)
    thread.start()
    return thread


def drain_inflight_dispatch(dispatcher, timeout_s=DRAIN_TIMEOUT_S, clock=time.monotonic, sleep=time.sleep):
    """On stop, never start new claims and let an in-flight claim finish.

    Intake 4398ce2a: host releases restart the service; killing the daemon
    thread between claim and native start dropped claims. Bounded so the
    supervisor's grace period still applies. Returns True when drained.
    """
    dispatcher.stop_requested.set()
    deadline = clock() + timeout_s
    while getattr(dispatcher, '_inflight', 0) > 0 and clock() < deadline:
        sleep(0.2)
    drained = getattr(dispatcher, '_inflight', 0) <= 0
    print(json.dumps({'event': 'dispatch_drain', 'drained': drained}), flush=True)
    return drained


def maybe_start_maintenance_thread(current, dispatcher, quota_collector):
    # Single inflight preserves Fleet dispatch and quota ownership. No extra
    # controller or native-effect receiver, and no unbounded executor pool.
    if current is not None and current.is_alive():
        return current
    thread = threading.Thread(target=maintenance_step_once,
                              args=(dispatcher, quota_collector),
                              daemon=True, name='fleet-maintenance')
    thread.start()
    return thread


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
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt
            if os.fstat(lock.fileno()).st_size == 0:
                lock.write('0')
                lock.flush()
            os.lseek(lock.fileno(), 0, os.SEEK_SET)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        settings = fleet.load_settings(settings_path)
        native = settings.get('aplexer_bin', 'aplexer')
        binding = qualify_sender_binding(native, str(Path.cwd().resolve()))
        # M15d narrow plumbing: surface the trusted cleanup-root allowlist
        # size (no auto-delete daemon here; tree removal is explicit CLI only).
        roots = get_cleanup_roots(settings)
        print(json.dumps({'event': 'host_ready', 'native_controller': binding.session_id,
                          'cleanup_roots': len(roots)}), flush=True)
        stop = threading.Event()
        previous = {}
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, lambda *_: stop.set())
        # Resolve trusted role_config independently from consumer_enabled.
        # Explicit settings["role_config"] (path, dict, or TrustedRoleConfig)
        # or defaults to journal / "roles.json" if present.
        role_cfg_path = None
        role_cfg_obj = None
        if "role_config" in settings:
            rc_val = settings["role_config"]
            if isinstance(rc_val, str):
                role_cfg_path = rc_val
            elif isinstance(rc_val, dict):
                try:
                    role_cfg_obj = load_role_config(rc_val)
                except Exception:
                    role_cfg_obj = None
            elif isinstance(rc_val, TrustedRoleConfig):
                role_cfg_obj = rc_val
        else:
            journal_roles = journal / "roles.json"
            if journal_roles.exists():
                role_cfg_path = str(journal_roles)

        dispatcher = fleet.Dispatcher(
            settings,
            role_config=role_cfg_obj,
            role_config_path=role_cfg_path,
            journal_dir=str(journal),
        )
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
        # Metrics producer hook (M9 freshness): ONE instance for the controller
        # lifetime, trusted config under the existing journal, this machine,
        # >=60s cadence (default 5min). Missing config means disabled. The loop
        # below only starts/skips the single-inflight daemon thread.
        metrics_collector = MetricsProducer(
            config_path=str(journal / 'metrics-producer.json'),
            machine_id=cfg['machine_id'])
        metrics_thread: Optional[threading.Thread] = None
        # Disk monitoring hook (Addendum 6): ONE instance for the controller
        # lifetime, trusted config under the existing journal, this machine,
        # rate-limited (default 60s). Missing config means disabled. The loop
        # below only starts/skips the single-inflight daemon thread.
        disk_collector = DiskMonitor(
            config_path=str(journal / 'disk-monitor.json'),
            machine_id=cfg['machine_id'])
        disk_thread: Optional[threading.Thread] = None
        # Host health monitoring hook (Addendum 6b): ONE instance for the controller
        # lifetime, trusted config under the existing journal, this machine,
        # rate-limited (default 60s). The loop below only starts/skips the
        # single-inflight daemon thread.
        host_health_collector = HostHealthMonitor(
            config_path=str(journal / 'host-health.json'),
            machine_id=cfg['machine_id'])
        host_health_thread: Optional[threading.Thread] = None
        # Role consumer hook (M8b Platform Role Runtime): ONE instance for the controller
        # lifetime, disabled by default. If enabled in settings,
        # runs single-inflight inside existing supervisor loop.
        rc_setting = settings.get("role_consumer", {})
        rc_enabled = False
        rc_interval = 10.0
        if isinstance(rc_setting, dict):
            rc_enabled = bool(rc_setting.get("enabled", False))
            if "interval_sec" in rc_setting:
                rc_interval = float(rc_setting["interval_sec"])
        elif isinstance(rc_setting, bool):
            rc_enabled = rc_setting

        role_consumer = RoleConsumer(
            config_path=role_cfg_path,
            role_config=role_cfg_obj,
            base_url=cfg.get("base_url"),
            host_id=cfg.get("machine_id"),
            min_interval_s=rc_interval,
            enabled=rc_enabled,
        )
        role_consumer_thread: Optional[threading.Thread] = None
        # Native token usage hook (usage-history intake): ONE instance for the
        # controller lifetime, hourly cadence, journal state file. Only
        # complete hours are posted; identical reposts are idempotent
        # server-side. The loop below only starts/skips the single-inflight
        # daemon thread.
        token_collector = TokenCollector(
            base_url=cfg.get('base_url'),
            state_path=str(journal / 'token-usage-state.json'))
        token_thread: Optional[threading.Thread] = None
        next_fleet = 0
        maintenance_thread = None
        next_telemetry = 0
        telemetry_thread = None
        next_offers = 0
        offers_thread = None
        bindings_thread = None
        count = 0
        # Operator fleet settings (workspaces map, workspace_roots,
        # subscriptions, launcher) are re-read when the file changes, so a
        # newly added repo becomes placeable without a host restart. The
        # re-registration in the next fleet cycle then advertises the fresh
        # workspace list to the Hub. Invalid edits fail closed on the last
        # good settings (see maybe_reload_fleet_settings).
        settings_mtime = fleet_settings_mtime(settings_path)
        try:
            bindings_thread = maybe_start_named_thread(
                None, bindings_loop, (dispatcher, stop), 'fleet-bindings')
            while not stop.is_set() and (cycles is None or count < cycles):
                # Apply settings between maintenance rounds, so an in-flight
                # dispatch cannot mix the old allowlist with new credentials.
                reloaded = None
                if maintenance_thread is None or not maintenance_thread.is_alive():
                    reloaded, settings_mtime = maybe_reload_fleet_settings(settings_path, settings_mtime)
                if reloaded is not None:
                    settings = reloaded
                    dispatcher.settings = reloaded
                    native = reloaded.get('aplexer_bin', 'aplexer')
                    roots = get_cleanup_roots(reloaded)
                    print(json.dumps({'event': 'settings_reloaded'}), flush=True)
                if time.monotonic() >= next_fleet:
                    maintenance_thread = maybe_start_maintenance_thread(
                        maintenance_thread, dispatcher, quota_collector)
                    next_fleet = time.monotonic() + fleet_interval
                # Intake 4398ce2a: telemetry and offer claiming must not wait
                # for the multi-minute maintenance cycle.
                if time.monotonic() >= next_telemetry:
                    telemetry_thread = maybe_start_named_thread(
                        telemetry_thread, telemetry_step_once, (dispatcher,), 'fleet-telemetry')
                    next_telemetry = time.monotonic() + fleet.TELEMETRY_REFRESH_S
                if time.monotonic() >= next_offers:
                    offers_thread = maybe_start_named_thread(
                        offers_thread, offers_step_once, (dispatcher,), 'fleet-offers')
                    next_offers = time.monotonic() + fleet.OFFERS_POLL_S
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
                        cleanup_roots=roots, settings=None))
                except Exception as error:
                    print(json.dumps({'event': 'commands_error', 'type': type(error).__name__}), flush=True)
                # Status observation never blocks commands/quota: one bounded
                # single-inflight daemon thread, skipped while alive.
                status_thread = maybe_start_status_thread(status_thread, status_collector)
                # Metrics production never blocks commands/quota: one bounded
                # single-inflight daemon thread, skipped while alive.
                metrics_thread = maybe_start_metrics_thread(metrics_thread, metrics_collector)
                # Disk monitoring never blocks commands/quota: one bounded
                # single-inflight daemon thread, skipped while alive.
                disk_thread = maybe_start_disk_thread(disk_thread, disk_collector)
                # Host health monitoring never blocks commands/quota: one bounded
                # single-inflight daemon thread, skipped while alive.
                host_health_thread = maybe_start_host_health_thread(host_health_thread, host_health_collector)
                # Role consumption never blocks commands/quota: one bounded
                # single-inflight daemon thread, skipped while alive.
                role_consumer_thread = maybe_start_role_consumer_thread(role_consumer_thread, role_consumer)
                # Native token collection never blocks commands/quota: one
                # bounded single-inflight daemon thread, skipped while alive.
                token_thread = maybe_start_token_thread(token_thread, token_collector)
                count += 1
                if cycles is None or count < cycles:
                    stop.wait(interval)
        finally:
            stop.set()
            try:
                drain_inflight_dispatch(dispatcher)
            except Exception:
                pass
            if bindings_thread is not None:
                bindings_thread.join(timeout=1)
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
