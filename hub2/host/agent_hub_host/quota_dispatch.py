"""Owner-only quota-monitor dispatch/session coupling. No wire admission API.

Default broker poison is never cleared; even real bounded attempts cannot
qualify the preceding window/system/cron/other roles. Tests using fake manager
handles prove state transitions only, never source or dispatch authority.
"""
import threading
import queue
import time
import uuid
from . import alert_authority as ram


class DispatchOffer:
    __slots__ = ('source','deadline','ready','ticket','failed')
    def __init__(self, source, deadline):
        self.source, self.deadline = source, deadline
        self.ready, self.ticket, self.failed = threading.Event(), None, False
    def await_owner(self, now=time.monotonic):
        self.ready.wait(max(0, self.deadline-now()))
        # Caller must start ORIGINAL selected business once even when None.
        return self.ticket if self.ready.is_set() and not self.failed and now()<self.deadline else None


class Held:
    __slots__ = ('owner', 'context', 'epoch', 'source', 'deadline', 'lease', 'opened',
                 'terminal', 'released', 'retired', 'fault')
    def __init__(self, owner, source, deadline):
        self.owner, self.context, self.epoch = owner, uuid.uuid4().hex, owner.ram.epoch
        self.source, self.deadline, self.lease = source, deadline, None
        self.opened = self.terminal = self.released = self.retired = False
        self.fault = None


class QuotaDispatch:
    """All methods execute on the SAME resident broker owner thread.

    Only the trusted manager creates source/process/channel leases. Clients can
    submit hook/snapshot as before but cannot construct these internal objects.
    Source observations never create a reservation. No healthy epoch reset.
    """
    def __init__(self, broker, manager, *, now=time.monotonic):
        self.broker, self.ram, self.manager, self.now = broker, broker.ram, manager, now
        self.thread = threading.get_ident()
        self.active, self.seen = {}, set()
        self.pending = queue.Queue(maxsize=ram.MAX_ACTIVE)
        self.pending_loss = threading.Event()
        self.closed = False
    def owner(self):
        if threading.get_ident() != self.thread:
            self.ram.collector_lost({'quota_stale'})
            raise ram.Unknown('owner_thread_lost')
    def fail(self, ticket, reason='collector_lost'):
        self.owner()
        if ticket is not None and ticket.fault is None: ticket.fault = reason
        self.ram.poison({'quota_stale'}, reason)
    def checked(self, ticket, *, budget=True):
        self.owner()
        ram.require(type(ticket) is Held and ticket.owner is self and self.active.get(ticket.context) is ticket
                    and ticket.epoch == self.ram.epoch and not self.closed and not ticket.retired)
        if budget: ram.require(self.now() < ticket.deadline)
    def post(self, source):
        # Trusted dispatcher in-process ONLY; no RAM mutation off owner thread.
        offer = DispatchOffer(source, self.now()+.15)
        try:
            if self.closed: raise queue.Full()
            self.pending.put_nowait(offer)
        except queue.Full:
            self.pending_loss.set(); offer.failed=True; offer.ready.set()
        return offer
    def reserve(self, source, deadline=None):
        self.owner()
        try:
            ram.require(not self.closed and len(self.active) < ram.MAX_ACTIVE and len(self.seen) < ram.MAX_LIFETIME)
            # Source is an actual manager-held selection/FD, not a caller dict.
            end = self.now()+.15 if deadline is None else deadline
            ram.require(self.now() < end)
            self.manager.source_current(source)
            ram.require(self.now() < end)
            ram.require(source.pin == self.ram.pins['quota_monitor'])
            ticket = Held(self, source, end)
            ram.require(ticket.context not in self.seen)
            self.seen.add(ticket.context); self.active[ticket.context] = ticket
            return ticket
        except BaseException:
            self.fail(None)
            raise
    def bind_started(self, ticket, channel):
        self.owner()
        try:
            self.checked(ticket)
            # Must finish inside original150ms hold. Manager verifies actual
            # still-held acknowledgement, same unit/Invocation/cgroup/ExecStart,
            # live pidfd +birth, interpreter and peer before any source release.
            lease = self.manager.bind_held(ticket.source, channel, ticket.deadline)
            self.checked(ticket); self.manager.lease_current(lease)
            self.manager.source_current(ticket.source)
            self.manager.drain_before_open(lease, ticket.deadline)
            self.checked(ticket)
            self.ram.admit(ticket.context, 'quota_monitor', None, lease.process, ticket.source.pin)
            ticket.lease = lease
            return True
        except BaseException as error:
            self.fail(ticket)
            if not isinstance(error, Exception): raise
            return False
    def source_open(self, ticket, event):
        self.owner()
        try:
            self.checked(ticket)
            ram.require(ticket.lease is not None and not ticket.opened and ticket.fault is None)
            self.manager.lease_current(ticket.lease); self.manager.source_current(ticket.source)
            # Real kernel record from the marked source, not wrapper's claimed
            # digest/message. Manager validates event origin and live pidfd.
            self.manager.exact_open(ticket.lease, ticket.source, event)
            self.ram.source_open('quota_monitor', ticket.source.pin, ticket.lease.process.pid, ticket.lease.process)
            self.checked(ticket); ticket.opened = True
            return True
        except BaseException as error:
            self.fail(ticket, 'unmatched_source_open')
            if not isinstance(error, Exception): raise
            return False
    def release(self, ticket):
        self.owner()
        # Called once on every path. Failure means fail-open BUSINESS ONCE,
        # not retrying start/effect; wrapper's own same deadline is independent.
        self.checked(ticket, budget=False)
        if ticket.released: return False
        ticket.released = True
        qualified = (ticket.fault is None and ticket.opened and self.now() < ticket.deadline)
        try:
            self.manager.release_once(ticket.lease, qualified, ticket.context, ticket.deadline)
        except BaseException as error:
            self.fail(ticket)
            if not isinstance(error, Exception): raise
            return False
        if not qualified: self.fail(ticket)
        return qualified
    def hook(self, ticket, channel, frame):
        self.owner()
        try:
            self.checked(ticket, budget=False)
            ram.require(ticket.fault is None and ticket.opened and ticket.released and ticket.lease is not None)
            self.manager.channel_current(ticket.lease, channel)
            self.manager.lease_current(ticket.lease); self.manager.source_current(ticket.source)
            reply = self.ram.hook(frame, ticket.context, ticket.lease.process)
            if reply.get('ok') is not True: self.fail(ticket, 'hook_unqualified')
            if frame.get('phase') == 'terminal' and reply.get('ok') is True: ticket.terminal = True
            return reply
        except BaseException as error:
            self.fail(ticket, 'hook_unqualified')
            if not isinstance(error, Exception): raise
            return {'seq': -1, 'ok': False}
    def retire(self, ticket):
        self.owner()
        try:
            self.checked(ticket, budget=False)
            ram.require(ticket.lease is not None)
            # Original held pidfd EXIT +same manager connection final barrier
            # and exact Invocation/exit status, never terminal hook alone.
            status = self.manager.retired(ticket.lease)
            self.manager.source_current(ticket.source)
            self.ram.process_exit(ticket.context, ticket.lease.process, status)
            if not ticket.terminal: self.fail(ticket, 'missing_terminal')
        except BaseException as error:
            self.fail(ticket, 'missing_terminal')
            if not isinstance(error, Exception): raise
        finally:
            ticket.retired = True; self.active.pop(ticket.context, None)
    def owns(self, context):
        self.owner(); return context in self.seen
    def public_hook(self, context):
        self.owner()
        # Even the right PID/context over an arbitrary client connection cannot
        # replace the originally bound source-session channel.
        self.fail(self.active.get(context), 'hook_unqualified')
        return {'seq': -1, 'ok': False}
    def unmatched_open(self): self.fail(None, 'unmatched_source_open')
    def barrier(self):
        self.owner(); start = self.now()
        try:
            ram.require(not self.active and self.pending.empty() and not self.pending_loss.is_set() and not self.closed)
            self.manager.barrier(start + .15)
            ram.require(self.now() - start <= .15)
            return True
        except BaseException as error:
            self.fail(None)
            if not isinstance(error, Exception): raise
            return False
    def step(self):
        self.owner()
        if self.pending_loss.is_set(): self.fail(None)
        # At most one queued request per tick; bounded128 pending +4096 lifetime.
        try: offer = self.pending.get_nowait()
        except queue.Empty: offer = None
        if offer is not None:
            try: offer.ticket = self.reserve(offer.source, offer.deadline)
            except Exception: offer.failed = True
            finally: offer.ready.set()
        self.manager.step(self)
    def close(self):
        self.owner(); self.closed = True
        first = None
        try:
            while True:
                try: offer = self.pending.get_nowait()
                except queue.Empty: break
                offer.failed=True; offer.ready.set(); self.fail(None)
            for ticket in list(self.active.values()):
                try:
                    self.fail(ticket)
                    if not ticket.released:
                        ticket.released = True
                        self.manager.release_once(ticket.lease, False, ticket.context, ticket.deadline)
                except BaseException as error:
                    if first is None: first = error
        finally:
            try: self.manager.close()
            except BaseException as error:
                if first is None: first = error
        if first is not None: raise first


def configured_same_process(value):
    """Strict opt-in config validator for same-process quota dispatch (default OFF)."""
    from pathlib import Path
    import os
    try:
        ram.require(type(value) is dict and set(value) == {
            'v', 'enabled', 'broker_directory', 'gate_directory',
            'source_directory', 'unit', 'cgroup',
        })
        ram.require(type(value['v']) is int and value['v'] == 1 and value['enabled'] is True)
        for key in ('broker_directory', 'gate_directory', 'source_directory'):
            directory = value[key]
            ram.require(type(directory) is str and directory.startswith('/')
                        and '\x00' not in directory and '..' not in Path(directory).parts
                        and len(os.fsencode(directory)) <= 80)
        unit = value['unit']
        cgroup = value['cgroup']
        ram.require(type(unit) is str and unit.endswith('.service') and '/' not in unit and len(unit) <= 128)
        ram.require(type(cgroup) is str and cgroup.startswith('/') and '\x00' not in cgroup and len(cgroup) <= 256)
        return dict(value)
    except Exception:
        return None


def run_same_process_quota(
    prepared=None,
    launcher=None,
    *,
    enabled=False,
    broker_directory=None,
    collector_factory=None,
    identity=None,
    now=time.monotonic,
    probe_factory=None,
    snapshot_fn=None,
):
    """Opt-in, default-OFF same-process quota dispatch lifecycle caller.

    Selection reservation precedes launch. Original selected business runs
    once even if reservation/broker fails. Never claims complete=True.
    """
    if not enabled:
        return {'enabled': False, 'dispatched': False, 'authority': 'NONE', 'complete': False}

    from . import alert_broker as wire
    from .quota_dispatch_manager import PreparedQuotaSelection, SerialProbe, V4QuotaManager

    ram.require(type(prepared) is PreparedQuotaSelection and callable(launcher))
    ram.require(broker_directory is not None)

    state = {'dispatch': None, 'manager': None, 'ready': threading.Event(), 'error': None}
    cleanup_lock = threading.Lock()
    cleaned = {'done': False}

    def _cleanup_once():
        with cleanup_lock:
            if cleaned['done']:
                return
            cleaned['done'] = True
        prepared.cleanup_owned()

    def _dispatch_factory(broker):
        if broker.stop_event.is_set():
            _cleanup_once()
            raise ram.Unknown('broker_stopped')
        try:
            mgr = V4QuotaManager(
                prepared,
                broker.collector,
                broker.events,
                now=now,
                probe_factory=probe_factory or SerialProbe,
            )
            disp = QuotaDispatch(broker, mgr, now=now)
            state['manager'] = mgr
            state['dispatch'] = disp
            return disp
        except BaseException as err:
            state['error'] = err
            raise
        finally:
            state['ready'].set()

    kwargs = {
        'collector_factory': collector_factory or (lambda cb: wire.Broker._collector(None, cb)),
        'dispatch_factory': _dispatch_factory,
    }
    if identity is not None:
        kwargs['identity'] = identity

    broker = None
    launch_result = None
    snapshot = None
    ticket = None
    mgr = None
    primary_error = None
    try:
        try:
            broker = wire.Broker._selected_quota_attempt(broker_directory, prepared.source, **kwargs)
            broker.start()
            while not state['ready'].is_set() and not broker.closed and now() < prepared.service_deadline:
                time.sleep(0.002)
            mgr = state['manager']
            disp = state['dispatch']
            while (mgr is not None and mgr.phase in ('initial_begin', 'initial_poll')
                   and not broker.closed and now() < prepared.service_deadline):
                time.sleep(0.002)
            if disp is not None and mgr is not None and mgr.phase == 'accept' and not broker.closed:
                offer = disp.post(prepared.source)
                ticket = offer.await_owner(now=now)
        except Exception:
            # Observer/broker setup failure fails open once; primary exceptions propagate.
            try:
                launch_result = launcher(None)
            except BaseException as err:
                primary_error = err
                raise
            return {
                'enabled': True,
                'dispatched': False,
                'reserved': False,
                'phase': mgr.phase if mgr is not None else 'failed',
                'launch_result': launch_result,
                'snapshot': None,
                'complete': False,
            }

        # Selection reservation precedes launch; run original selected business ONCE.
        # Never swallow or retry original primary exceptions.
        try:
            launch_result = launcher(ticket)
        except BaseException as err:
            primary_error = err
            raise

        try:
            while (mgr is not None and mgr.phase not in ('done', 'failed')
                   and not broker.closed and now() < prepared.service_deadline):
                time.sleep(0.002)
            if not broker.closed and broker.listener is not None:
                snap_call = snapshot_fn or wire.exchange
                snapshot = snap_call(broker.path, {'v': 1, 'op': 'snapshot', 'nonce': uuid.uuid4().hex})
        except Exception:
            snapshot = None
        return {
            'enabled': True,
            'dispatched': True,
            'reserved': ticket is not None,
            'phase': mgr.phase if mgr is not None else 'failed',
            'launch_result': launch_result,
            'snapshot': snapshot,
            'complete': False,
        }
    finally:
        try:
            wire.stop_optional(broker)
            owner_gone = broker is None or broker.thread is None or not broker.thread.is_alive()
            if state['manager'] is None and owner_gone:
                _cleanup_once()
        except BaseException:
            if primary_error is None:
                raise
