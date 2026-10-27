"""A timer's slow launch must not serialize unrelated fast-loop claims."""
import threading
from pathlib import Path
from unittest.mock import patch

from agent_hub_host import fleet
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
