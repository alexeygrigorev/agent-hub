"""Intake eff9a5ce burst fix: bounded parallel offer dispatch, single-flight
admission gate, one dispatch per launch id, and offers-read timeouts that hold
instead of failing."""
import contextlib
import io
import json
import tempfile
import threading
import time
import pytest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from agent_hub_host import fleet

try:
    from host.tests.test_fleet import settings, offer
except ImportError:  # pragma: no cover - direct host/tests invocation
    from test_fleet import settings, offer


def _gate_runner(argv, **kw):
    return {'launch_allowed': True, 'profile_supported': True,
            'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}


def _offers(n):
    return [offer(id=f"{n:02d}{i:06d}-1234-1234-1234-123456789abc") for i in range(n)]


def test_dispatch_concurrency_default_and_bounds():
    assert fleet.dispatch_concurrency({}) == fleet.DISPATCH_CONCURRENCY == 10
    assert fleet.dispatch_concurrency({'dispatch_concurrency': 50}) == fleet.DISPATCH_CONCURRENCY_MAX == 10
    assert fleet.dispatch_concurrency({'dispatch_concurrency': 0}) == 1
    assert fleet.dispatch_concurrency({'dispatch_concurrency': 'x'}) == 10


def test_burst_of_ten_dispatches_in_parallel_bounded():
    with tempfile.TemporaryDirectory() as tmp:
        items = _offers(10)

        def request(method, path, body=None):
            if method == 'GET' and path == '/api/fleet/offers':
                return {'offers': items}
            return {}
        d = fleet.Dispatcher(settings(Path(tmp)), request, _gate_runner, journal_dir=tmp)
        active, peak, done, lock = [0], [0], [], threading.Lock()

        def slow(o, results):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.3)
            with lock:
                active[0] -= 1
                done.append(o['id'])
            results.append({'id': o['id'], 'state': 'running'})
        with patch.object(d, '_dispatch_one', side_effect=slow):
            started = time.monotonic()
            out = d.offers_tick()
            elapsed = time.monotonic() - started
        assert out['offers'] == 10
        assert sorted(done) == sorted(o['id'] for o in items)  # each exactly once
        assert len(done) == 10
        assert peak[0] == 10, peak
        assert elapsed < 1.5, elapsed  # serial would be >= 3.0 s
        assert d._inflight == 0 and not d._dispatching


def test_duplicate_offer_ids_dispatch_once():
    with tempfile.TemporaryDirectory() as tmp:
        one = _offers(1)[0]
        d = fleet.Dispatcher(settings(Path(tmp)), lambda *a, **k: {}, _gate_runner, journal_dir=tmp)
        calls = []
        with patch.object(d, '_dispatch_one', side_effect=lambda o, r: calls.append(o['id'])):
            d.dispatch_offers([], offers=[one, dict(one), dict(one)], concurrency=6)
        assert calls == [one['id']]


def test_offer_already_dispatching_in_process_is_held_not_redispatched():
    with tempfile.TemporaryDirectory() as tmp:
        one = _offers(1)[0]
        d = fleet.Dispatcher(settings(Path(tmp)), lambda *a, **k: {}, _gate_runner, journal_dir=tmp)
        d._dispatching.add(one['id'])
        results = []
        with patch.object(d, '_dispatch_one', side_effect=AssertionError('double dispatch')):
            d.dispatch_offers(results, offers=[one], concurrency=6)
        assert results == [{'id': one['id'], 'state': 'held', 'reason': 'dispatch_in_progress'}]


def test_admission_gate_single_flight_and_shared_briefly():
    with tempfile.TemporaryDirectory() as tmp:
        runs, lock = [0], threading.Lock()

        def runner(argv, **kw):
            with lock:
                runs[0] += 1
            time.sleep(0.2)
            return _gate_runner(argv)
        cfg = settings(Path(tmp))
        d = fleet.Dispatcher(cfg, lambda *a, **k: {}, runner, journal_dir=tmp)
        sub = {'admission_argv': ['maintained-gate'], 'account_label': 'shared-codex'}
        payloads = [{'mode': 'headless', 'engine': 'codex', 'profile': None, 'account': 'shared-codex',
                     'workspace': f'/ws{i}', 'selection': None} for i in range(8)]
        out = []
        threads = [threading.Thread(target=lambda p=p: out.append(d._dispatch_admission(sub, p, None, {})))
                   for p in payloads]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert runs[0] == 1, runs
        assert len(out) == 8
        # Different route (engine) is never shared.
        d._dispatch_admission(sub, dict(payloads[0], engine='opencode'), None, {})
        assert runs[0] == 2
        # Expired share re-runs the gate.
        with patch.object(fleet, 'ADMISSION_SHARE_S', 0.0):
            d._dispatch_admission(sub, payloads[0], None, {})
        assert runs[0] == 3


def test_admission_refusal_is_never_shared():
    with tempfile.TemporaryDirectory() as tmp:
        runs = [0]

        def runner(argv, **kw):
            runs[0] += 1
            return {'launch_allowed': False, 'profile_supported': True,
                    'account_label': 'shared-codex', 'observed_at': fleet.utcnow()}
        d = fleet.Dispatcher(settings(Path(tmp)), lambda *a, **k: {}, runner, journal_dir=tmp)
        sub = {'admission_argv': ['maintained-gate'], 'account_label': 'shared-codex'}
        p = {'mode': 'headless', 'engine': 'codex', 'profile': None, 'account': 'shared-codex', 'workspace': '/w', 'selection': None}
        for _ in range(3):
            try:
                d._dispatch_admission(sub, p, None, {})
            except ValueError:
                pass
        assert runs[0] == 3


def test_offers_fetch_retries_timeouts_then_succeeds():
    with tempfile.TemporaryDirectory() as tmp:
        attempts = [0]
        items = _offers(1)

        def request(method, path, body=None):
            if path == '/api/fleet/offers':
                attempts[0] += 1
                if attempts[0] < 3:
                    raise TimeoutError('The read operation timed out')
                return {'offers': items}
            return {}
        d = fleet.Dispatcher(settings(Path(tmp)), request, _gate_runner, journal_dir=tmp)
        dispatched = []
        buf = io.StringIO()
        with patch.object(fleet, 'OFFERS_FETCH_BACKOFF_S', 0.0), \
                patch.object(d, '_dispatch_one', side_effect=lambda o, r: dispatched.append(o['id'])), \
                contextlib.redirect_stdout(buf):
            out = d.offers_tick()
        assert attempts[0] == 3 and out['offers'] == 1 and dispatched == [items[0]['id']]
        events = [json.loads(l) for l in buf.getvalue().splitlines() if l.strip()]
        assert [e['attempt'] for e in events if e.get('event') == 'offers_fetch_retry'] == [1, 2]


def test_offers_fetch_definitive_error_is_not_retried():
    with tempfile.TemporaryDirectory() as tmp:
        attempts = [0]

        def request(method, path, body=None):
            attempts[0] += 1
            raise ValueError('HTTP request failed with status 403: forbidden')
        d = fleet.Dispatcher(settings(Path(tmp)), request, _gate_runner, journal_dir=tmp)
        try:
            d._fetch_offers()
        except ValueError:
            pass
        else:
            raise AssertionError('expected ValueError')
        assert attempts[0] == 1


def test_offers_fetch_exhausted_timeouts_never_reject_or_claim():
    with tempfile.TemporaryDirectory() as tmp:
        calls = []

        def request(method, path, body=None):
            calls.append((method, path))
            if path == '/api/fleet/offers':
                raise TimeoutError('timed out')
            return {}
        d = fleet.Dispatcher(settings(Path(tmp)), request, _gate_runner, journal_dir=tmp)
        with patch.object(fleet, 'OFFERS_FETCH_BACKOFF_S', 0.0), contextlib.redirect_stdout(io.StringIO()):
            try:
                d.offers_tick()
            except TimeoutError:
                pass
        assert calls == [('GET', '/api/fleet/offers')] * fleet.OFFERS_FETCH_ATTEMPTS


@pytest.mark.parametrize('age,status,expected', [
    (1300, 'ok', 'unknown'), (-30, 'ok', 'unknown'),
    (None, 'ok', 'unknown'), (60, 'ok', 'ok'),
    (1300, 'unknown', 'unknown'),
])
def test_subscription_freshness_cannot_turn_stale_or_future_into_fresh(tmp_path, age, status, expected):
    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)
    observed = (now - dt.timedelta(seconds=age)).isoformat() if age is not None else None
    quota = {'status': status, 'observed_at': observed, 'windows': {},
             'details': {'cooldown_until': '2026-11-01T00:00:00Z'}}
    rows = [{'account': 'shared-codex', 'service': 'codex', 'plan': 'pro', 'quota': quota}]
    calls = []
    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]), \
            patch.object(fleet, 'collect_subscriptions', return_value=rows):
        dispatcher = fleet.Dispatcher(settings(tmp_path), lambda m, p, b: calls.append(b), lambda *a: {})
        dispatcher._report_subscriptions()
    sent = calls[0]['subscriptions'][0]['quota']
    assert sent['status'] == expected
    if expected != status or age is None or age < 0:
        assert sent['observed_at'] is None
    else:
        assert sent['observed_at'] == observed
    assert sent['details'] == quota['details']  # known cooldown is never erased


def test_subscription_server_timestamp_refusal_retries_once_with_unknown(tmp_path):
    rows = [{'account': 'shared-codex', 'service': 'codex', 'plan': 'pro',
             'quota': {'status': 'ok', 'observed_at': fleet.utcnow(), 'windows': {}, 'details': {}}}]
    requests = []

    def request(method, path, body):
        requests.append(body)
        if len(requests) == 1:
            raise ValueError('HTTP request failed with status 400: subscription_unmapped_or_invalid:invalid_timestamp')
        return {'status': 'reported'}

    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]), \
            patch.object(fleet, 'collect_subscriptions', return_value=rows):
        dispatcher = fleet.Dispatcher(settings(tmp_path), request, lambda *a: {})
        assert dispatcher._report_subscriptions() == {'status': 'reported'}
    assert len(requests) == 2
    assert requests[0]['subscriptions'][0]['quota']['status'] == 'ok'
    assert requests[1]['subscriptions'][0]['quota']['status'] == 'unknown'
    assert requests[1]['subscriptions'][0]['quota']['observed_at'] is None


@pytest.mark.parametrize('error', ['HTTP request failed with status 403',
                                  'HTTP request failed with status 400: subscription_unmapped_or_invalid:unmapped'])
def test_other_subscription_refusals_never_retry_or_relax_validation(tmp_path, error):
    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]), \
            patch.object(fleet, 'collect_subscriptions', return_value=[]):
        from unittest.mock import Mock
        request = Mock(side_effect=ValueError(error))
        dispatcher = fleet.Dispatcher(settings(tmp_path), request, lambda *a: {})
        with pytest.raises(ValueError):
            dispatcher._report_subscriptions()
        assert request.call_count == 1


def test_census_sampled_before_start_never_suppresses_its_followup(tmp_path):
    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
        dispatcher = fleet.Dispatcher(settings(tmp_path), lambda *a: {}, lambda *a: {}, journal_dir=str(tmp_path))
    first_census, finish = threading.Event(), threading.Event()
    calls = []

    def census():
        calls.append(1)
        if len(calls) == 1:
            first_census.set()
            assert finish.wait(5)
        return list(calls)

    with patch.object(dispatcher, '_report_catalog_bindings', side_effect=census):
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(dispatcher.report_catalog_bindings)
            assert first_census.wait(5)
            second = pool.submit(dispatcher.report_catalog_bindings)
            time.sleep(.02)
            finish.set()
            first.result(timeout=5)
            second.result(timeout=5)
    assert len(calls) == 2


def test_catalog_transport_failure_is_retried_without_cached_success(tmp_path):
    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
        dispatcher = fleet.Dispatcher(settings(tmp_path), lambda *a: {}, lambda *a: {})
    with patch.object(dispatcher, '_report_catalog_bindings', side_effect=[ValueError('temporary'), []]) as report:
        with pytest.raises(ValueError):
            dispatcher.report_catalog_bindings()
        assert dispatcher.report_catalog_bindings() == []
        assert report.call_count == 2


def test_ten_overlapping_auth_checks_share_success_but_next_call_verifies_again(tmp_path):
    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
        d = fleet.Dispatcher(settings(tmp_path), lambda *a: {}, lambda *a: {})
    ready = threading.Event()
    lock = threading.Lock()
    entered = 0

    class Guard:
        def __enter__(self):
            lock.acquire()

        def __exit__(self, *args):
            nonlocal entered
            entered += 1
            if entered >= 10:
                ready.set()
            lock.release()

    d._device_auth_guard = Guard()

    def verify(cfg):
        assert ready.wait(5)  # Every call has recorded its request timestamp.
        return {'status': 'ok'}

    with patch('agent_hub_host.opencode_auth.prepare', side_effect=verify) as prepare:
        with ThreadPoolExecutor(10) as pool:
            futures = [pool.submit(d._prepare_device_auth, {'target_data': str(tmp_path)})
                       for _ in range(10)]
            for future in futures:
                future.result(timeout=5)
        assert prepare.call_count == 1
        d._prepare_device_auth({'target_data': str(tmp_path)})
        assert prepare.call_count == 2


def test_auth_failures_are_never_shared_and_changed_config_verifies(tmp_path):
    with patch('agent_hub_host.provisioning.managed_subscriptions', return_value=[]):
        d = fleet.Dispatcher(settings(tmp_path), lambda *a: {}, lambda *a: {})
    with patch('agent_hub_host.opencode_auth.prepare',
               side_effect=[ValueError('engine_auth_expired'), {}, {}]) as prepare:
        with pytest.raises(ValueError, match='engine_auth_expired'):
            d._prepare_device_auth({'target_data': str(tmp_path)})
        d._prepare_device_auth({'target_data': str(tmp_path)})
        d._prepare_device_auth({'target_data': str(tmp_path / 'different')})
        assert prepare.call_count == 3
