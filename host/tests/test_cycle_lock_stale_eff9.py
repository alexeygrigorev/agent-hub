"""Intake 8cc2848c (rework of the eff9a5ce follow-up): the fleet cycle never
skips wholesale. The former whole-cycle lock starved the 60s fleet-host timer
behind a long-running session_host cycle (~2000 fleet_cycle_skipped
concurrent_cycle/24h). Only the stateful slow-maintenance phase stays
single-flight now: a fresh live holder costs one fleet_maintenance_skipped
receipt while heartbeat, offers and the rest of the cycle still run; a wedged
holder (pid gone, or held past cycle_lock_stale_s) is taken over."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import fleet

try:
    from host.tests.test_fleet import settings
except ImportError:  # pragma: no cover - direct host/tests invocation
    from test_fleet import settings


def _base_request_factory(calls):
    def request(method, path, body=None):
        calls.append((method, path))
        if method == 'GET' and path == '/api/fleet/offers':
            return {'offers': []}
        return {}
    return request


def _base_runner(argv, **kw):
    if argv and argv[0] == 'maintained-gate':
        return {'launch_allowed': True, 'profile_supported': True,
                'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
    return []


def _stubbed_once(dispatcher, reconcile_runs):
    """Run once() with heavy seams stubbed; reconcile_launches records into
    reconcile_runs so tests can tell a skipped phase from a run one."""
    buf = io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch('agent_hub_host.provisioning.tick'))
        stack.enter_context(patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]))
        stack.enter_context(patch('agent_hub_host.system.get_system_info', return_value={'capabilities': {}}))
        stack.enter_context(patch('agent_hub_host.api.get_heartbeat_payload', return_value={}))
        stack.enter_context(patch('agent_hub_host.workclaim.sync_once', return_value={'ok': True}))
        stack.enter_context(patch.object(fleet, 'collect_subscriptions', return_value=[]))
        for method, value in [('report_catalog_bindings', []), ('reconcile_imported_sessions', []),
                              ('_derive_qualified_role_jobs', [])]:
            stack.enter_context(patch.object(dispatcher, method, return_value=value))
        stack.enter_context(patch.object(
            dispatcher, 'reconcile_launches',
            side_effect=lambda *a, **k: reconcile_runs.append(1) or []))
        with contextlib.redirect_stdout(buf):
            results = dispatcher.once()
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    return results, events


def _rewrite_owner(path, pid, acquired_at):
    # Same open file semantics as a holder rewriting its metadata.
    with open(path, 'r+b') as fh:
        fh.truncate()
        fh.write(json.dumps({'pid': pid, 'acquired_at': acquired_at}).encode())


def test_acquire_records_holder_pid_and_time():
    with tempfile.TemporaryDirectory() as tmp:
        d = fleet.Dispatcher(settings(Path(tmp)), lambda *a, **k: {}, _base_runner, journal_dir=tmp)
        before = time.time()
        held = d._acquire_cycle_lock()
        try:
            owner = json.loads((Path(tmp) / fleet.CYCLE_LOCK_NAME).read_text())
            assert owner['pid'] == os.getpid()
            assert before - 1 <= owner['acquired_at'] <= time.time() + 1
            info = d._cycle_lock_holder()
            assert info['holder_pid'] == os.getpid() and info['holder_alive'] is True
            assert info['stale'] is False
        finally:
            held.close()


def test_fresh_live_holder_skips_only_the_maintenance_phase():
    with tempfile.TemporaryDirectory() as tmp:
        calls = []
        cfg = settings(Path(tmp))
        holder = fleet.Dispatcher(cfg, lambda *a, **k: {}, _base_runner, journal_dir=tmp)
        held = holder._acquire_cycle_lock()
        try:
            loser = fleet.Dispatcher(cfg, _base_request_factory(calls), _base_runner, journal_dir=tmp)
            runs: list = []
            results, events = _stubbed_once(loser, runs)
            # The cycle itself ran: heartbeat and offers happened normally.
            assert not any(e.get('event') == 'fleet_cycle_skipped' for e in events)
            assert ('GET', '/api/fleet/offers') in calls, calls
            assert ('POST', '/api/host/poll') in calls, calls
            assert not any(r.get('reason') == 'concurrent_cycle' for r in results), results
            # Only the guarded maintenance phase skipped, with a receipt.
            skip = next(e for e in events if e.get('event') == 'fleet_maintenance_skipped')
            assert skip['reason'] == 'concurrent_maintenance'
            assert skip['holder_pid'] == os.getpid() and skip['held_s'] < 60
            assert runs == [], 'reconcile must not run while the sibling owns the phase'
            phases = next(e for e in events if e.get('event') == 'fleet_cycle_phases')['phases']
            assert 'reconcile' in phases and 'watchdog' in phases
            assert not any(e.get('event') == 'fleet_maintenance_lock_takeover' for e in events)
        finally:
            held.close()


def test_live_holder_past_stale_threshold_is_taken_over():
    with tempfile.TemporaryDirectory() as tmp:
        calls = []
        cfg = settings(Path(tmp))
        holder = fleet.Dispatcher(cfg, lambda *a, **k: {}, _base_runner, journal_dir=tmp)
        held = holder._acquire_cycle_lock()
        try:
            path = Path(tmp) / fleet.CYCLE_LOCK_NAME
            _rewrite_owner(path, os.getpid(), time.time() - fleet.CYCLE_LOCK_STALE_S - 5)
            loser = fleet.Dispatcher(cfg, _base_request_factory(calls), _base_runner, journal_dir=tmp)
            runs: list = []
            results, events = _stubbed_once(loser, runs)
            take = next(e for e in events if e.get('event') == 'fleet_maintenance_lock_takeover')
            assert take['holder_pid'] == os.getpid() and take['holder_alive'] is True
            assert take['held_s'] > fleet.CYCLE_LOCK_STALE_S
            assert not any(e.get('event') == 'fleet_maintenance_skipped' for e in events)
            assert runs, 'takeover must run the maintenance phase instead of skipping forever'
            assert ('GET', '/api/fleet/offers') in calls, calls
        finally:
            held.close()


def test_dead_holder_pid_is_taken_over():
    proc = subprocess.Popen([sys.executable, '-c', 'pass'])
    proc.wait()
    dead_pid = proc.pid
    with tempfile.TemporaryDirectory() as tmp:
        calls = []
        cfg = settings(Path(tmp))
        holder = fleet.Dispatcher(cfg, lambda *a, **k: {}, _base_runner, journal_dir=tmp)
        held = holder._acquire_cycle_lock()  # e.g. fd inherited by a surviving child
        try:
            _rewrite_owner(Path(tmp) / fleet.CYCLE_LOCK_NAME, dead_pid, time.time())
            loser = fleet.Dispatcher(cfg, _base_request_factory(calls), _base_runner, journal_dir=tmp)
            runs: list = []
            results, events = _stubbed_once(loser, runs)
            take = next(e for e in events if e.get('event') == 'fleet_maintenance_lock_takeover')
            assert take['holder_pid'] == dead_pid and take['holder_alive'] is False
            assert runs
            assert ('GET', '/api/fleet/offers') in calls, calls
        finally:
            held.close()


def test_legacy_holder_without_metadata_is_never_stale():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = settings(Path(tmp))
        holder = fleet.Dispatcher(cfg, lambda *a, **k: {}, _base_runner, journal_dir=tmp)
        held = holder._acquire_cycle_lock()
        try:
            with open(Path(tmp) / fleet.CYCLE_LOCK_NAME, 'r+b') as fh:
                fh.truncate()
            calls = []
            loser = fleet.Dispatcher(cfg, _base_request_factory(calls), _base_runner, journal_dir=tmp)
            runs: list = []
            results, events = _stubbed_once(loser, runs)
            skip = next(e for e in events if e.get('event') == 'fleet_maintenance_skipped')
            assert skip['reason'] == 'concurrent_maintenance'
            assert runs == []
            # The cycle still ran end to end despite the pre-upgrade holder.
            assert ('GET', '/api/fleet/offers') in calls, calls
        finally:
            held.close()


def test_stale_threshold_setting_override():
    cfg = settings(Path('/repo'))
    cfg['cycle_lock_stale_s'] = 30
    assert fleet.Dispatcher(cfg, lambda *a, **k: {}, _base_runner)._cycle_lock_stale_s() == 30.0
    cfg['cycle_lock_stale_s'] = 'bogus'
    assert fleet.Dispatcher(cfg, lambda *a, **k: {}, _base_runner)._cycle_lock_stale_s() == fleet.CYCLE_LOCK_STALE_S
