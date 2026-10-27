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


def test_controller_does_not_reload_settings_during_offer_read_or_startup(tmp_path):
    from contextlib import ExitStack
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock

    cfg = settings(Path(tmp_path))
    cfg['revision_marker'] = 'old'
    dispatcher = fleet.Dispatcher(cfg, lambda *a: {}, lambda *a: {}, journal_dir=str(tmp_path))
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(cfg))
    fetching, release_fetch = threading.Event(), threading.Event()
    launching, release_launch = threading.Event(), threading.Event()
    raced = threading.Event()
    unchanged = []
    readers = []
    commands = []
    original_start = session_host.maybe_start_named_thread

    def fetch():
        if fetching.is_set():
            return {'offers': []}
        fetching.set()
        assert release_fetch.wait(5)
        return {'offers': [offer(id='reload-race-offer')]}

    def dispatch(item, results):
        snapshot = dispatcher.settings
        assert snapshot['revision_marker'] == 'old'
        launching.set()
        assert release_launch.wait(5)
        unchanged.append(dispatcher.settings is snapshot)

    def reload_settings(path, mtime):
        if not fetching.is_set():
            return None, mtime
        if not release_fetch.is_set():
            # Old guard enters load_settings while GET is still in flight.
            # Let that GET spawn an old-settings worker before loading ends.
            raced.set()
            release_fetch.set()
            assert launching.wait(2)
        return {**cfg, 'revision_marker': 'new'}, mtime

    def start(current, target, args, name):
        if name != 'fleet-offers':
            return None
        reader = original_start(current, target, args, name)
        readers.append(reader)
        return reader

    def command(_):
        commands.append(True)
        if len(commands) == 1:
            assert fetching.wait(2)
        elif len(commands) == 2:
            release_fetch.set()
            try:
                assert launching.wait(2)
                assert not raced.is_set(), 'settings reload overlapped the offer reader'
            except BaseException:
                release_launch.set()
                raise
        elif len(commands) == 3:
            assert dispatcher.settings['revision_marker'] == 'old'
            release_launch.set()
            wait_idle(dispatcher)
        else:
            assert dispatcher.settings['revision_marker'] == 'new'

    with ExitStack() as stack:
        stack.enter_context(patch.object(session_host.config, 'load_config', return_value={
            'machine_id': 'synthetic-machine', 'access_token': 'synthetic'}))
        stack.enter_context(patch.object(session_host.fleet, 'load_settings', return_value=cfg))
        stack.enter_context(patch.object(session_host, 'qualify_sender_binding', return_value=SimpleNamespace(
            session_id='synthetic-controller', workspace=str(tmp_path))))
        stack.enter_context(patch.object(session_host.fleet, 'Dispatcher', return_value=dispatcher))
        stack.enter_context(patch.object(session_host, 'QuotaCollector', return_value=Mock()))
        stack.enter_context(patch.object(session_host, 'maybe_reload_fleet_settings', side_effect=reload_settings))
        stack.enter_context(patch.object(session_host, 'maybe_start_maintenance_thread', return_value=None))
        stack.enter_context(patch.object(session_host, 'maybe_start_named_thread', side_effect=start))
        stack.enter_context(patch.object(session_host.session_control, 'execute', side_effect=command))
        stack.enter_context(patch.object(dispatcher, '_fetch_offers', side_effect=fetch))
        stack.enter_context(patch.object(dispatcher, '_dispatch_one', side_effect=dispatch))
        for name in ('status', 'metrics', 'disk', 'host_health', 'role_consumer', 'token'):
            stack.enter_context(patch.object(session_host, 'maybe_start_' + name + '_thread', return_value=None))
        try:
            session_host.run(str(path), str(tmp_path / 'controller'), interval=1, cycles=4)
        finally:
            release_fetch.set()
            release_launch.set()
            for reader in readers:
                reader.join(5)
            wait_idle(dispatcher)
    assert len(commands) == 4
    assert unchanged == [True]
    assert not raced.is_set()
    assert dispatcher.settings['revision_marker'] == 'new'
