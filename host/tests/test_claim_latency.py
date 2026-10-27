"""A timer's slow launch must not serialize unrelated fast-loop claims."""
import threading
import time
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import fleet
from agent_hub_host import offer_claims, session_host
from test_fleet import settings, offer


def test_claim_not_blocked_by_timer_cycle(tmp_path):
    cfg = settings(Path(tmp_path))
    timer = fleet.Dispatcher(cfg, lambda *a: {}, lambda *a: {}, journal_dir=str(tmp_path))
    fast = fleet.Dispatcher(cfg, lambda *a: {}, lambda *a: {}, journal_dir=str(tmp_path))
    first = offer(id='timer-offer')
    second = offer(id='fresh-offer')
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow(item, results):
        entered.set()
        assert release.wait(5)

    cycle_lock = timer._acquire_cycle_lock()
    assert cycle_lock is not False
    worker = threading.Thread(target=lambda: timer.dispatch_offers([], offers=[first]))
    try:
        with patch.object(timer, '_dispatch_one', side_effect=slow), patch.object(
                fast, '_dispatch_one', side_effect=lambda o, r: calls.append(o['id'])):
            worker.start()
            assert entered.wait(5)
            # Same launch is still single-flight across controller instances;
            # the new launch proceeds while both timer locks are held.
            fast.dispatch_offers([], offers=[first, second], concurrency=2)
            assert calls == ['fresh-offer']
    finally:
        release.set()
        worker.join(5)
        if cycle_lock is not None:
            cycle_lock.close()
    assert not worker.is_alive()
    # Locks are released on completion so retries remain possible.
    with patch.object(fast, '_dispatch_one', side_effect=lambda o, r: calls.append(o['id'])):
        fast.dispatch_offers([], offers=[first])
    assert calls == ['fresh-offer', 'timer-offer']


def wait_idle(dispatcher):
    deadline = time.monotonic() + 5
    while offer_claims.has_inflight(dispatcher) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not offer_claims.has_inflight(dispatcher)
    assert dispatcher._inflight == 0


def test_controller_polls_new_retry_while_prior_batch_startup_is_blocked(tmp_path):
    cfg = settings(Path(tmp_path))
    dispatcher = fleet.Dispatcher(cfg, lambda *a: {}, lambda *a: {}, journal_dir=str(tmp_path))
    first, retry = offer(id='slow-first-batch'), offer(id='fresh-retry')
    entered, release, retried = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def start(item, results):
        calls.append(item['id'])
        if item['id'] == first['id']:
            entered.set()
            assert release.wait(5)
        else:
            retried.set()

    reader = None
    try:
        with patch.object(dispatcher, '_fetch_offers', return_value={'offers': [first]}) as fetch, patch.object(
                dispatcher, '_dispatch_one', side_effect=start):
            reader = session_host.maybe_start_named_thread(
                None, session_host.offers_step_once, (dispatcher,), 'test-offer-poll')
            assert entered.wait(2)
            reader.join(0.5)
            fetch.return_value = {'offers': [first, retry]}
            # The real controller's single-inflight polling thread must now
            # be free, even though the first launch still owns its offer lock.
            reader = session_host.maybe_start_named_thread(
                reader, session_host.offers_step_once, (dispatcher,), 'test-offer-poll')
            assert retried.wait(1)
            assert not release.is_set()
            assert calls.count(first['id']) == 1
    finally:
        release.set()
        if reader is not None:
            reader.join(5)
        wait_idle(dispatcher)


def test_poll_workers_are_bounded_without_a_stale_offer_queue_and_drain_on_stop(tmp_path):
    cfg = settings(Path(tmp_path))
    cfg['dispatch_concurrency'] = 2
    dispatcher = fleet.Dispatcher(cfg, lambda *a: {}, lambda *a: {}, journal_dir=str(tmp_path))
    releases = {name: threading.Event() for name in ['a', 'b', 'd']}
    entered = {name: threading.Event() for name in ['a', 'b', 'c', 'd']}
    calls = []

    def start(item, results):
        name = item['id']
        calls.append(name)
        entered[name].set()
        if name in releases:
            assert releases[name].wait(5)

    try:
        with patch.object(dispatcher, '_fetch_offers', return_value={
                'offers': [offer(id='a'), offer(id='b'), offer(id='c')]}) as fetch, patch.object(
                dispatcher, '_dispatch_one', side_effect=start):
            offer_claims.poll(dispatcher)
            assert entered['a'].wait(2) and entered['b'].wait(2)
            assert not entered['c'].is_set()
            # C is withdrawn before a worker is free. There must be no queued
            # C launch that later bypasses the next authoritative offers read.
            fetch.return_value = {'offers': [offer(id='b'), offer(id='d')]}
            releases['a'].set()
            deadline = time.monotonic() + 2
            while not entered['d'].is_set() and time.monotonic() < deadline:
                offer_claims.poll(dispatcher)
                entered['d'].wait(0.01)
            assert entered['d'].is_set()
            assert calls.count('b') == 1
            assert not entered['c'].is_set()
            dispatcher.stop_requested.set()
            fetch.reset_mock()
            offer_claims.poll(dispatcher)
            fetch.assert_not_called()
    finally:
        for release in releases.values():
            release.set()
        wait_idle(dispatcher)
