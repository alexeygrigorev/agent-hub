"""Keep polling offers while bounded, independent launch workers are busy.

There is no startup queue: offers without a free worker stay on the Hub and
are read again. Every worker uses the ordinary dispatcher, including its
per-offer process/kernel locks and all admission and claim gates.
"""
from collections import deque
import json
import threading
import time
_STATES_LOCK = threading.Lock()


class _State:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = {}
        self.completed = deque(maxlen=100)


def _state(dispatcher):
    with _STATES_LOCK:
        state = vars(dispatcher).get('_offer_claim_state')
        if state is None:
            state = dispatcher._offer_claim_state = _State()
        return state


def has_inflight(dispatcher):
    with _STATES_LOCK:
        state = vars(dispatcher).get('_offer_claim_state')
    if state is None:
        return False
    with state.lock:
        return bool(state.running)


def _run(dispatcher, state, offer, token):
    results = []
    started = time.monotonic()
    try:
        dispatcher.dispatch_offers(results, offers=[offer], concurrency=1,
                                   refresh=False)
    except Exception as error:
        # The dispatcher owns launch-state reconciliation. An unexpected
        # observer error cannot manufacture a failed/held Hub result.
        print(json.dumps({'event': 'offer_worker_error',
                          'type': type(error).__name__}), flush=True)
    finally:
        with state.lock:
            if state.running.get(offer['id']) is token:
                del state.running[offer['id']]
            state.completed.append((results, time.monotonic() - started))


def poll(dispatcher):
    """One short poll, without joining any launcher startup or auth check."""
    from .fleet import dispatch_concurrency
    state = _state(dispatcher)
    with state.lock:
        completed = list(state.completed)
        state.completed.clear()
        available = dispatch_concurrency(dispatcher.settings) - len(state.running)
    out = {'offers': 0,
           'results': [result for results, _ in completed for result in results],
           'dispatch_max_s': round(max((duration for _, duration in completed), default=0), 3)}
    if dispatcher.stop_requested.is_set() or available <= 0:
        return out

    # The same brief fetch lock is shared with the maintained timer cycle.
    # It is released before any worker can enter native startup.
    lock = dispatcher._acquire_offers_lock()
    if lock is False:
        return out
    try:
        offers = dispatcher._fetch_offers().get('offers', [])
    finally:
        if lock is not None:
            lock.close()
    out['offers'] = len(offers)
    for offer in offers:
        oid = offer.get('id') if isinstance(offer, dict) else None
        if not isinstance(oid, str) or not oid or dispatcher.stop_requested.is_set():
            continue
        with state.lock:
            if len(state.running) >= dispatch_concurrency(dispatcher.settings):
                break
            if oid in state.running:
                continue
            token = object()
            state.running[oid] = token
        worker = threading.Thread(target=_run, args=(dispatcher, state, offer, token),
                                  name='fleet-offer-start', daemon=True)
        try:
            worker.start()
        except BaseException:
            with state.lock:
                if state.running.get(oid) is token:
                    del state.running[oid]
            raise
    return out
