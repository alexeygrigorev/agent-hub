"""Intake 4398ce2a: host telemetry/offer cadence must be independent of the
multi-minute fleet cycle and consistent with the Hub stale windows."""
import contextlib
import inspect
import io
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import fleet, session_host
try:
    from host.tests.test_fleet import settings, offer
except ImportError:  # PYTHONPATH=host/tests
    from test_fleet import settings, offer

REPO = Path(__file__).resolve().parents[2]


def _gate_runner(argv, **kw):
    if argv and argv[0] == 'maintained-gate':
        return {'launch_allowed': True, 'profile_supported': True,
                'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
    return {'active_mem_mb': 512}


def _hub_stale_ms():
    text = (REPO / 'src' / 'fleet.ts').read_text()
    match = re.search(r'export const LAUNCHER_TELEMETRY_STALE_MS = (\d+);', text)
    assert match, 'LAUNCHER_TELEMETRY_STALE_MS missing from src/fleet.ts'
    return int(match.group(1))


def test_refresh_interval_fits_hub_stale_window_with_margin():
    stale_s = _hub_stale_ms() / 1000
    assert fleet.HUB_TELEMETRY_STALE_S == stale_s
    # Two missed refreshes must still leave the telemetry fresh.
    assert fleet.TELEMETRY_REFRESH_S * 3 <= stale_s, (fleet.TELEMETRY_REFRESH_S, stale_s)
    assert fleet.OFFERS_POLL_S <= 30
    # Claim freshness on the Hub is 30 s; re-admission must trigger well before.
    assert fleet.CLAIM_ADMISSION_REFRESH_S < 30
    assert session_host.DRAIN_TIMEOUT_S < 30  # aplexer kill grace max 30000 ms
    sup = (REPO / "scripts" / "session-host-supervisor.py").read_text()
    grace = int(re.search(r"--grace-ms', '(\d+)'", sup).group(1))
    assert session_host.DRAIN_TIMEOUT_S * 1000 < grace <= 30000


def test_telemetry_tick_posts_fresh_resources_while_slow_cycle_holds_lock():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = settings(Path(tmp))
        cfg['launcher'] = {'argv': ['launcher'], 'config_dir': tmp, 'root': tmp}
        posts = []

        def request(method, path, body=None):
            posts.append((method, path, body))
            return {}
        dispatcher = fleet.Dispatcher(cfg, request, _gate_runner, journal_dir=tmp)
        held = dispatcher._acquire_cycle_lock()  # slow cycle in progress
        assert held not in (False, None)
        try:
            with patch('agent_hub_host.api.get_heartbeat_payload', return_value={'mem_avail_mb': 1}):
                hb = dispatcher.telemetry_tick()
        finally:
            held.close()
        polls = [b for m, p, b in posts if p == '/api/host/poll']
        assert polls, posts
        assert polls[0]['launcher_resources']['active_mem_mb'] == 512
        assert fleet._iso_age_s(polls[0]['launcher_resources']['observed_at']) < 5
        assert polls[0]['gate']['launch_allowed'] is True
        assert fleet._iso_age_s(polls[0]['gate']['at']) < 5
        assert hb is dispatcher._last_hb


def test_offers_tick_claims_while_slow_cycle_holds_lock_and_serialises():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = settings(Path(tmp))
        item = offer()

        def request(method, path, body=None):
            if method == 'GET' and path == '/api/fleet/offers':
                return {'offers': [item]}
            return {}
        dispatcher = fleet.Dispatcher(cfg, request, _gate_runner, journal_dir=tmp)
        dispatched = []
        held = dispatcher._acquire_cycle_lock()
        try:
            with patch.object(dispatcher, 'dispatch', side_effect=lambda o: dispatched.append(o['id']) or {'id': o['id'], 'state': 'held', 'reason': 'test'}):
                out = dispatcher.offers_tick()
                assert out['offers'] == 1 and dispatched == [item['id']]
                busy = dispatcher._acquire_offers_lock()
                try:
                    assert dispatcher.offers_tick()['offers'] == 0  # sibling dispatching
                finally:
                    busy.close()
                dispatcher.stop_requested.set()
                dispatcher.offers_tick()
                assert dispatched == [item['id']], 'no new claims while draining'
        finally:
            held.close()


def test_drain_waits_for_inflight_claim():
    class D:
        def __init__(self):
            import threading
            self.stop_requested = threading.Event()
            self._inflight = 1
    d = D()
    ticks = iter(range(100))

    def sleep(_):
        d._inflight = 0
    with contextlib.redirect_stdout(io.StringIO()):
        assert session_host.drain_inflight_dispatch(d, timeout_s=5, clock=lambda: next(ticks), sleep=sleep)
    assert d.stop_requested.is_set()


def test_claim_readmits_when_first_observation_ages():
    src = inspect.getsource(fleet.Dispatcher._dispatch_after_admission) if hasattr(fleet.Dispatcher, '_dispatch_after_admission') else inspect.getsource(fleet.Dispatcher.dispatch)
    claim = src.index('path + "/claim"')
    readmit = src.rfind('CLAIM_ADMISSION_REFRESH_S', 0, claim)
    assert readmit != -1 and '_dispatch_admission' in src[readmit:claim]


def test_offers_tick_dispatches_concurrently_and_refresh_posts_fresh_heartbeat():
    import threading
    import time as _time
    with tempfile.TemporaryDirectory() as tmp:
        cfg = settings(Path(tmp))
        items = [offer(id=f"1234567{n}-1234-1234-1234-123456789abc") for n in range(3)]
        polls = []

        def request(method, path, body=None):
            if method == "GET" and path == "/api/fleet/offers":
                return {"offers": items}
            if path == "/api/host/poll":
                polls.append(body)
            return {}
        cfg["dispatch_concurrency"] = 3
        dispatcher = fleet.Dispatcher(cfg, request, _gate_runner, journal_dir=tmp)
        dispatcher._last_hb = {"gate": {"launch_allowed": True, "at": "old"}, "cpu_usage": {"observed_at": "stale"}}
        active, peak, lock = [0], [0], threading.Lock()

        def slow_dispatch(o):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            _time.sleep(0.2)
            with lock:
                active[0] -= 1
            return {"id": o["id"], "state": "held", "reason": "test"}
        with patch.object(dispatcher, "dispatch", side_effect=slow_dispatch), \
                patch("agent_hub_host.api.get_heartbeat_payload", return_value={"cpu_usage": {"observed_at": "fresh"}}):
            out = dispatcher.offers_tick()
            assert out["offers"] == 3 and len(out["results"]) == 3
            assert peak[0] >= 2, "opt-in concurrency must dispatch in parallel"
            assert polls == [], "fast tick relies on the telemetry thread for check-in"
            dispatcher._refresh_checkin({"cpu_usage": {"observed_at": "stale"}})
        assert polls and all(p["cpu_usage"]["observed_at"] == "fresh" for p in polls)
        assert all(p["gate"]["launch_allowed"] is True for p in polls)
