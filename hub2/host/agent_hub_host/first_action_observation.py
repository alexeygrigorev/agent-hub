"""Bounded, late first-action observation.

First-action proof is later activity evidence, never a start precondition. The
native receipt is returned immediately; one daemon worker then re-checks the
exact attempt's artifact and publishes a proven timestamp once. All filesystem
work happens on that worker, never on the receive/heartbeat/stop thread. Unproven,
stale, replaced, stopped or expired watches simply drop: UNKNOWN is never
promoted to success.
"""
import logging
import threading
import time

MAX_WATCHES = 16
MAX_LATCHED = 64
POLL_SECONDS = 2.0
PUBLISH_ATTEMPTS = 3


class _Watch:
    __slots__ = ('native_id', 'deadline', 'prove', 'current', 'publish', 'attempts')

    def __init__(self, native_id, deadline, prove, current, publish):
        self.native_id, self.deadline = native_id, deadline
        self.prove, self.current, self.publish = prove, current, publish
        self.attempts = 0


class FirstActionObserver:
    def __init__(self, interval=POLL_SECONDS, limit=MAX_WATCHES, clock=time.time, on_exit=None):
        self.interval, self.limit, self.clock, self.on_exit = interval, limit, clock, on_exit
        self._lock = threading.Lock()
        self._watches = {}   # task_id -> _Watch; a retry replaces the older attempt
        self._latched = {}   # native_id -> proven activity ms, insertion ordered
        self._thread = None
        self._stop = threading.Event()

    def watch(self, task_id, native_id, deadline, prove, current, publish):
        """Register without blocking. False when the bounded table is full.

        prove() -> proven activity in epoch ms, or None for unknown.
        current() -> whether the exact attempt/fence is still live.
        publish(ms) -> True when finished (stored or definitively refused).
        """
        with self._lock:
            if task_id not in self._watches and len(self._watches) >= self.limit:
                return False
            self._watches[task_id] = _Watch(native_id, deadline, prove, current, publish)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name='hub2-first-action', daemon=True)
                self._thread.start()
        return True

    def cancel(self, task_id):
        with self._lock:
            self._watches.pop(task_id, None)

    def activity(self, native_id):
        """In-memory lookup only; safe on the heartbeat thread."""
        with self._lock:
            return self._latched.get(native_id)

    def close(self):
        self._stop.set()

    def _run(self):
        try:
            while not self._stop.is_set():
                with self._lock:
                    items = list(self._watches.items())
                    if not items:
                        self._thread = None
                        return
                for task_id, watch in items:
                    self._step(task_id, watch)
                self._stop.wait(self.interval)
        finally:
            with self._lock:
                if self._thread is threading.current_thread():
                    self._thread = None
            if self.on_exit is not None:
                try:
                    self.on_exit()
                except Exception:
                    pass

    def _drop(self, task_id, watch):
        with self._lock:
            if self._watches.get(task_id) is watch:
                del self._watches[task_id]

    def _step(self, task_id, watch):
        try:
            if self.clock() >= watch.deadline or not watch.current():
                return self._drop(task_id, watch)
            stamp = watch.prove()
            if stamp is None:
                return
            with self._lock:
                # A retry/replacement or cancel during proof wins; nothing latches.
                if self._watches.get(task_id) is not watch:
                    return
                del self._watches[task_id]
            if not watch.current():
                return  # STOP/replacement landed during the proof read.
            with self._lock:
                self._latched[watch.native_id] = stamp
                while len(self._latched) > MAX_LATCHED:
                    del self._latched[next(iter(self._latched))]
            self._publish(task_id, watch, stamp)
        except Exception:
            logging.getLogger(__name__).warning('First-action observation unavailable task=%s', task_id)
            self._drop(task_id, watch)

    def _publish(self, task_id, watch, stamp):
        try:
            if watch.publish(stamp):
                return
        except Exception:
            pass
        watch.attempts += 1
        if watch.attempts < PUBLISH_ATTEMPTS:
            with self._lock:
                self._watches.setdefault(task_id, watch)
            watch.prove = lambda value=stamp: value
        else:
            logging.getLogger(__name__).warning('First-action evidence update unavailable task=%s', task_id)
