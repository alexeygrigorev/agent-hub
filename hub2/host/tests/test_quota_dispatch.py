"""Pure owner-state controls. Fake manager handles are NOT runtime admission."""
import importlib.util
import os
from pathlib import Path
import struct
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from agent_hub_host import alert_authority as ram, alert_broker as wire
from agent_hub_host.quota_dispatch import QuotaDispatch
from agent_hub_host.quota_dispatch_manager import V4QuotaManager

PIN=ram.SourcePin(1,2,'a'*64)
KEY=ram.ProcessKey(321,55,'11111111-2222-4333-8444-555555555555')
class FakeManager:
    def __init__(self):
        self.source=SimpleNamespace(pin=PIN);self.channel=object();self.alive=True
        self.lease=SimpleNamespace(process=KEY,channel=self.channel)
        self.releases=[];self.bad_open=False;self.lost=False
    def source_current(self,s):
        if s is not self.source or self.lost:raise ram.Unknown('source_lost')
    def bind_held(self,s,c,end):
        if c is not self.channel:raise ram.Unknown('helper_pid_or_channel')
        return self.lease
    def lease_current(self,l):
        if not self.alive or l is not self.lease:raise ram.Unknown('process_lost')
    def drain_before_open(self,*a):
        if self.lost:raise ram.Unknown('collector_lost')
    def exact_open(self,*a):
        if self.bad_open:raise ram.Unknown('unmatched_open')
    def release_once(self,*a):self.releases.append(a)
    def channel_current(self,l,c):
        if c is not l.channel:raise ram.Unknown('channel_mismatch')
    def retired(self,l):
        if self.alive:raise ram.Unknown('not_retired')
        return 0
    def barrier(self,*a):
        if self.lost:raise ram.Unknown('collector_lost')
    def step(self,*a):pass
    def close(self):pass

class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.clock=0.;self.manager=FakeManager()
        self.broker=SimpleNamespace(ram=ram.LiveRAM({role:PIN for role in ram.ROLES},wire.checked,now=lambda:100))
        self.broker.ram.poison(ram.PRODUCERS,'dispatch_unqualified')
        self.owner=QuotaDispatch(self.broker,self.manager,now=lambda:self.clock)
        for target in ['socket.socket','subprocess.Popen','subprocess.run','os.pidfd_open','ctypes.CDLL']:
            p=patch(target,side_effect=AssertionError('runtime forbidden'));p.start();self.addCleanup(p.stop)
    def start(self):
        t=self.owner.reserve(self.manager.source)
        self.assertTrue(self.owner.bind_started(t,self.manager.channel))
        self.assertTrue(self.owner.source_open(t,object()));self.assertTrue(self.owner.release(t));return t
    def frame(self,t,seq):
        return dict(v=1,invocation_id=t.context,parent_id=None,role='quota_monitor',phase=['begin','decision','terminal'][seq],seq=seq,decision='observation' if seq==1 else None,condition='quota_fresh' if seq==1 else None,flags={},recipient_class='none',observer_ok=True)
    def test_core_math_success_never_clears_constructor_poison_or_grants_coverage(self):
        t=self.start()
        for n in range(3):self.assertTrue(self.owner.hook(t,self.manager.channel,self.frame(t,n))['ok'])
        self.manager.alive=False;self.owner.retire(t)
        self.assertEqual(self.broker.ram.counts['quota_stale'],{'started':1,'finished':1,'occurrences':0})
        proof=self.broker.ram.barrier('b'*32,self.owner.barrier)
        self.assertFalse(proof['complete']);self.assertFalse(proof['ram_consistent'])
        self.assertEqual(proof['faults']['quota_stale'],'dispatch_unqualified')
    def test_post_has_no_admission_and_expired_offer_cannot_resurrect(self):
        offer=self.owner.post(self.manager.source)
        self.assertFalse(self.broker.ram.active);self.clock=1;self.owner.step()
        self.assertIsNone(offer.await_owner(now=lambda:self.clock));self.assertFalse(self.broker.ram.active)
    def test_queued_reservation_uses_original_deadline_not_renewal(self):
        offer=self.owner.post(self.manager.source);self.clock=.1;self.owner.step()
        self.assertIs(offer.await_owner(now=lambda:self.clock),offer.ticket)
        self.assertEqual(offer.ticket.deadline,.15)
        self.clock=.16;self.assertFalse(self.owner.bind_started(offer.ticket,self.manager.channel))
    def test_pre_hook_exit_and_later_good_math_do_not_heal(self):
        t=self.owner.reserve(self.manager.source);self.owner.bind_started(t,self.manager.channel)
        self.manager.alive=False;self.owner.retire(t)
        self.assertEqual(self.broker.ram.counts['quota_stale']['finished'],0)
        self.assertEqual(t.fault,'missing_terminal')
        self.manager.alive=True;u=self.start()
        self.assertIsNotNone(self.broker.ram.faults['quota_stale']);self.assertFalse(self.broker.ram.barrier('c'*32,lambda:True)['complete'])
    def test_open_before_binding_wrong_open_and_read_without_reservation_refuse(self):
        t=self.owner.reserve(self.manager.source);self.assertFalse(self.owner.source_open(t,object()))
        self.assertFalse(self.owner.release(t));self.assertEqual(len(self.manager.releases),1)
        self.owner.unmatched_open();self.assertFalse(self.broker.ram.active)
    def test_helper_or_other_channel_never_substitutes_business_peer(self):
        t=self.owner.reserve(self.manager.source);self.assertFalse(self.owner.bind_started(t,object()))
        self.assertFalse(self.broker.ram.active)
        u=self.start();self.assertFalse(self.owner.hook(u,object(),self.frame(u,0))['ok'])
        self.assertFalse(self.owner.hook(u,self.manager.channel,self.frame(u,0))['ok'])
    def test_replay_and_observer_false_are_sticky(self):
        t=self.start();frame=self.frame(t,0)
        self.assertTrue(self.owner.hook(t,self.manager.channel,frame)['ok'])
        self.assertFalse(self.owner.hook(t,self.manager.channel,frame)['ok'])
        self.assertFalse(self.owner.hook(t,self.manager.channel,self.frame(t,1))['ok'])
        self.assertEqual(self.broker.ram.counts['quota_stale']['occurrences'],0)
    def test_terminal_is_not_retirement_and_loss_never_finishes(self):
        t=self.start()
        for n in range(3):self.owner.hook(t,self.manager.channel,self.frame(t,n))
        self.owner.retire(t);self.assertEqual(self.broker.ram.counts['quota_stale']['finished'],0)
        self.assertEqual(t.fault,'missing_terminal')
    def test_public_bound_context_is_refused_without_calling_hook(self):
        t=self.start();reply=self.owner.public_hook(t.context)
        self.assertIs(reply['ok'],False);self.assertFalse(self.owner.hook(t,self.manager.channel,self.frame(t,0))['ok'])
    def test_owner_thread_and_capacity_loss_cannot_be_reset(self):
        with patch('agent_hub_host.quota_dispatch.threading.get_ident',return_value=-1):
            with self.assertRaises(ram.Unknown):self.owner.reserve(self.manager.source)
        self.assertEqual(self.broker.ram.faults['quota_stale'],'dispatch_unqualified')
    def test_real_manager_rejects_serialized_proof_without_os_calls(self):
        with self.assertRaises(ram.Unknown):V4QuotaManager({'proof':True},None,[])
    def test_bootstrap_business_once_and_exact_error_identity(self):
        root=Path(__file__).resolve().parents[3]
        spec=importlib.util.spec_from_file_location('quota_bootstrap',root/'scripts/qualified-quota-bootstrap.py')
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
        calls=[];original=KeyboardInterrupt()
        def business(*a,**k):calls.append(1);raise original
        with self.assertRaises(KeyboardInterrupt) as caught:m.run_once(SimpleNamespace(main=business),[],None)
        self.assertIs(caught.exception,original);self.assertEqual(calls,[1])
        self.assertEqual(m.FALLBACK_QUOTA,(root/'scripts/hub-quota-publish.py').read_bytes())

    def test_observer_false_and_late_health_cannot_heal_session(self):
        t=self.start();frame=self.frame(t,0);frame['observer_ok']=False
        self.assertFalse(self.owner.hook(t,self.manager.channel,frame)['ok'])
        frame['observer_ok']=True
        self.assertFalse(self.owner.hook(t,self.manager.channel,frame)['ok'])
    def test_queue_capacity_refuses_evidence_not_original_business(self):
        for _ in range(ram.MAX_ACTIVE): self.owner.post(self.manager.source)
        extra=self.owner.post(self.manager.source)
        self.assertIsNone(extra.await_owner(now=lambda:self.clock))
        self.assertTrue(self.owner.pending_loss.is_set());self.assertFalse(self.broker.ram.active)
    def test_close_latches_interrupt_but_still_closes_manager_once(self):
        t=self.owner.reserve(self.manager.source);original=KeyboardInterrupt();closed=[]
        def release(*a):raise original
        self.manager.release_once=release;self.manager.close=lambda:closed.append(1)
        with self.assertRaises(KeyboardInterrupt) as caught:self.owner.close()
        self.assertIs(caught.exception,original);self.assertEqual(closed,[1])


class IncrementalManagerTests(unittest.TestCase):
    """Pure owner-loop scheduler/transport controls; NO thread or IO created."""
    def setUp(self):
        from agent_hub_host import quota_dispatch_manager as mod
        self.mod=mod;self.clock=0.;self.failures=[];self.releases=[];self.sent=[]
        self.channel=SimpleNamespace(setblocking=lambda flag:None,send=lambda raw:self.sent.append(raw) or len(raw),close=lambda:None)
        self.probe=SimpleNamespace(poll=lambda:None,submit=lambda *a:None,close=lambda:None)
        self.collector=SimpleNamespace(epoch='epoch',fault=None)
        self.ticket=SimpleNamespace(deadline=.15,released=True,context='a'*32,lease=SimpleNamespace(pidfd=4),terminal=False)
        self.owner=SimpleNamespace(active={'ticket':self.ticket},fail=lambda t:self.failures.append(t),release=lambda t:self.releases.append(t) or True,
            hook=lambda t,c,f:{'ok':True,'seq':f['seq']},retire=lambda t:None)
        m=object.__new__(mod.V4QuotaManager);self.m=m
        m.now=lambda:self.clock;m.p=SimpleNamespace(service_deadline=10.,poll_held=lambda:None)
        m.phase='exit_poll';m.closed=False;m.handled=False;m.collector=self.collector;m.ticket=self.ticket
        m.lease=self.ticket.lease;m.pending_channel=self.channel;m.probe=self.probe;m.probe_token=None
        m.barriers=SimpleNamespace(close=lambda:None);m.frame_deadline=.15;m.frame_seq=0;m.outbound=None
        m.channel_eof=False;m.last_hook_ok=False
        def pending(*a):raise BlockingIOError()
        self.channel.recvmsg=pending
        def hook(ticket,channel,frame):
            if frame.get('phase')=='terminal':ticket.terminal=True
            return {'ok':True,'seq':frame['seq']}
        self.owner.hook=hook
        m.stable=SimpleNamespace(alive=lambda fd:True);m.observation=SimpleNamespace(records=[{}],unknown=False)
        m.fan=SimpleNamespace(drain=lambda *a:None);m.group=3
        for target in ['socket.socket','subprocess.Popen','subprocess.run','os.pidfd_open','ctypes.CDLL','threading.Thread.start']:
            q=patch(target,side_effect=AssertionError('runtime forbidden'));q.start();self.addCleanup(q.stop)
    def test_waiting_exit_yields_every_turn_without_sleep_or_clock_advance(self):
        served=[]
        with patch('time.sleep',side_effect=AssertionError('owner sleep')):
            for _ in range(100):self.m.step(self.owner);served.append('observer-client')
        self.assertEqual(len(served),100);self.assertEqual(self.clock,0.)
        self.assertEqual(self.m.phase,'exit_poll');self.assertEqual(self.failures,[])
    def test_hook_arrival_after_slow_business_uses_service_lifetime_not_old_ack_deadline(self):
        self.clock=2.;self.m.phase='hook_receive';seen=[]
        self.m.packet=lambda deadline:seen.append(deadline)
        for _ in range(20):self.m.step(self.owner)
        self.assertEqual(seen,[10.]*20);self.assertEqual(self.m.phase,'hook_receive')
        self.assertEqual(self.m.frame_deadline,.15);self.assertEqual(self.failures,[])
    def test_received_frame_starts_one_ack_budget_and_next_turn_sends(self):
        import json
        self.clock=2.;self.m.phase='hook_receive'
        self.m.packet=lambda deadline:json.dumps({'v':1,'op':'hook','frame':{'seq':0}}).encode()
        self.m.step(self.owner)
        self.assertEqual(self.m.phase,'hook_send');self.assertEqual(self.sent,[]);self.assertEqual(self.m.frame_deadline,2.15)
        self.m.step(self.owner)
        self.assertEqual(len(self.sent),1);self.assertEqual(self.m.phase,'hook_receive');self.assertEqual(self.clock,2.)
    def test_three_frames_require_six_distinct_turns_with_clients_between(self):
        import json
        self.m.phase='hook_receive';served=[]
        self.m.packet=lambda deadline:json.dumps({'v':1,'op':'hook','frame':{'seq':self.m.frame_seq,'phase':['begin','decision','terminal'][self.m.frame_seq]}}).encode()
        for n in range(6):
            before=len(self.sent);self.m.step(self.owner);served.append(n)
            self.assertLessEqual(len(self.sent)-before,1)
        self.assertEqual(served,list(range(6)));self.assertEqual(self.m.phase,'exit_poll');self.assertEqual(len(self.sent),3)
    def test_accepted_channel_owned_before_any_banner_or_binding_failure(self):
        calls=[]
        def bad(flag):raise ValueError('no raw message serialized')
        self.channel.setblocking=bad;self.m.phase='accept';self.m.pending_channel=None
        self.m.p.poll_held=lambda:self.channel;self.probe.close=lambda:calls.append('cleanup_requested')
        self.m.step(self.owner)
        self.assertIs(self.m.pending_channel,self.channel);self.assertEqual(self.m.phase,'failed')
        self.assertEqual(calls,['cleanup_requested']);self.assertEqual(len(self.failures),1)
    def test_pending_probe_yields_and_expiry_discards_late_token_without_healing(self):
        self.m.phase='bind_poll';self.m.step(self.owner)
        self.assertEqual(self.m.phase,'bind_poll');self.assertEqual(self.clock,0.)
        self.clock=.16;self.probe.poll=lambda:self.mod.ProbeResult('bind',value=object())
        self.m.step(self.owner);self.assertEqual(self.m.phase,'failed')
        self.m.step(self.owner);self.assertEqual(len(self.failures),1)
    def test_service_deadline_expires_without_owner_loop_wait(self):
        self.clock=10.;self.m.step(self.owner)
        self.assertEqual(self.m.phase,'failed');self.assertEqual(self.clock,10.);self.assertEqual(len(self.failures),1)
    def test_control_identity_propagates_after_sticky_fault_and_cleanup_request(self):
        error=KeyboardInterrupt();calls=[]
        self.m.phase='accept';self.probe.close=lambda:calls.append('closed')
        def interrupt():raise error
        self.m.p.poll_held=interrupt
        with self.assertRaises(KeyboardInterrupt) as caught:self.m.step(self.owner)
        self.assertIs(caught.exception,error);self.assertEqual(calls,['closed']);self.assertEqual(self.m.phase,'failed')
    def test_ordered_barrier_sends_once_polls_same_pending_without_waits(self):
        calls=[]
        c=SimpleNamespace(pending=None,started=True,fault=None,manager=':1.2',jobs={},
            wire=SimpleNamespace(send=lambda *a:calls.append(a) or 7),receipt=lambda index:{'stream_index':index})
        b=self.mod.OrderedBarrier(c,lambda:self.clock);b.begin(.15)
        for _ in range(20):self.assertIsNone(b.poll())
        self.assertEqual(len(calls),1);self.assertEqual(self.clock,0.)
        c.pending['reply']=([],9);self.assertEqual(b.poll(),{'stream_index':9});self.assertIsNone(c.pending)
    def test_wrong_probe_operation_cannot_be_used_as_binding_fact(self):
        self.probe.poll=lambda:self.mod.ProbeResult('retire',value=object());self.m.phase='bind_poll'
        self.m.step(self.owner);self.assertEqual(self.m.phase,'failed');self.assertEqual(len(self.failures),1)


class AdapterBoundaryTests(unittest.TestCase):
    """Actual manager methods with fake stat/socket data, never real FD/IO."""
    def setUp(self):
        import hashlib,stat
        from agent_hub_host import quota_dispatch_manager as mod
        self.mod=mod;self.clock=0.;self.calls=[]
        self.info=SimpleNamespace(st_dev=11,st_ino=22,st_mode=stat.S_IFREG|0o400)
        self.pin=ram.SourcePin(11,22,hashlib.sha256(b'quota').hexdigest())
        self.held=mod.HeldSourceFD(3,4,'quota.py',b'quota',('stamp',),self.pin)
        m=object.__new__(mod.V4QuotaManager);self.m=m;m.now=lambda:self.clock
        m.source=self.held;m.closed=False;m.epoch='epoch';m.collector=SimpleNamespace(epoch='epoch',fault=None)
        m.p=SimpleNamespace(companions=(self.held,self.held))
        m.stable=SimpleNamespace(source_stamp=lambda info:('stamp',),checked_source=lambda *a:self.calls.append(a))
        for target in ['socket.socket','subprocess.Popen','subprocess.run','os.pidfd_open','ctypes.CDLL','threading.Thread.start']:
            q=patch(target,side_effect=AssertionError('runtime forbidden'));q.start();self.addCleanup(q.stop)
        for target in ('os.fstat','os.stat'):
            q=patch(target,return_value=self.info);q.start();self.addCleanup(q.stop)
    def select(self,pin=None,raw=b'quota'):
        self.held=self.mod.HeldSourceFD(3,4,'quota.py',raw,('stamp',),pin or self.pin)
        self.m.source=self.held;self.m.p.companions=(self.held,self.held)
    def test_live_fd_raw_pin_positive_in_owner_and_worker(self):
        self.m.source_current(self.held);self.m.full_sources()
        self.assertEqual(len(self.calls),3)
    def test_pin_with_wrong_live_fd_device_or_inode_refused_in_owner_and_worker(self):
        for pin in (ram.SourcePin(99,22,self.pin.sha256),ram.SourcePin(11,99,self.pin.sha256)):
            self.select(pin)
            for call in (lambda:self.m.source_current(self.held),self.m.full_sources):
                with self.assertRaises(ram.Unknown):call()
    def test_selected_pin_wrong_raw_digest_and_raw_replacement_refused(self):
        for pin,raw in ((ram.SourcePin(11,22,'f'*64),b'quota'),(self.pin,b'other-source')):
            self.select(pin,raw)
            for call in (lambda:self.m.source_current(self.held),self.m.full_sources):
                with self.assertRaises(ram.Unknown):call()
    def test_companion_pin_relation_not_exempted(self):
        bad=self.mod.HeldSourceFD(5,4,'client.py',b'quota',('stamp',),ram.SourcePin(11,99,self.pin.sha256))
        self.m.p.companions=(self.held,bad)
        with self.assertRaises(ram.Unknown):self.m.source_current(self.held)
        with self.assertRaises(ram.Unknown):self.m.full_sources()
    def test_recvmsg_explicit_zero_ancillary_capacity_and_nonblocking_flag(self):
        import socket
        calls=[]
        self.m.pending_channel=SimpleNamespace(recvmsg=lambda *a:calls.append(a) or (b'packet',[],0,None))
        self.assertEqual(self.m.packet(.15),b'packet')
        self.assertEqual(calls,[(2049,0,socket.MSG_DONTWAIT)])
    def test_control_or_packet_truncation_and_any_ancillary_refused(self):
        import socket
        for anc,flags in (([],socket.MSG_CTRUNC),([],socket.MSG_TRUNC),([('synthetic-control',)],0)):
            self.m.pending_channel=SimpleNamespace(recvmsg=lambda *a:(b'packet',anc,flags,None))
            with self.assertRaises(ram.Unknown):self.m.packet(.15)
    def test_nonblocking_empty_and_expired_packet_never_wait_or_read_late(self):
        calls=[]
        def pending(*args):calls.append(args);raise BlockingIOError()
        self.m.pending_channel=SimpleNamespace(recvmsg=pending)
        self.assertIsNone(self.m.packet(.15));self.clock=.16
        with self.assertRaises(ram.Unknown):self.m.packet(.15)
        self.assertEqual(len(calls),1)
    def test_source_session_constructor_uses_cached_epoch_without_collector_access(self):
        class Manager:
            epoch='cached-original-epoch'
            @property
            def collector(self):raise AssertionError('worker Collector access')
        lease=self.mod.SourceSession(Manager(),object(),3,object(),'invocation','cgroup',{})
        self.assertEqual(lease.epoch,'cached-original-epoch')
    def test_bootstrap_receiver_has_same_explicit_no_ancillary_contract(self):
        import socket
        root=Path(__file__).resolve().parents[3]
        spec=importlib.util.spec_from_file_location('quota_bootstrap_boundary',root/'scripts/qualified-quota-bootstrap.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        calls=[]
        with patch.object(module.time,'monotonic',return_value=0),patch.object(module.select,'select',return_value=([object()],[],[])):
            channel=SimpleNamespace(recvmsg=lambda *a:calls.append(a) or (b'packet',[],0,None))
            self.assertEqual(module.recv(channel,.15),b'packet');self.assertEqual(calls,[(2049,0,socket.MSG_DONTWAIT)])
            for anc,flags in (([],socket.MSG_CTRUNC),([],socket.MSG_TRUNC),([('synthetic-control',)],0)):
                channel.recvmsg=lambda *a:(b'packet',anc,flags,None)
                with self.assertRaises(RuntimeError):module.recv(channel,.15)


class PartialSelectionTests(unittest.TestCase):
    """Actual constructors/core paths, fake live-FD metadata; no runtime IO."""
    def setUp(self):
        import stat,hashlib
        from agent_hub_host.quota_dispatch_manager import HeldSourceFD
        self.pin=ram.SourcePin(11,22,hashlib.sha256(b'quota').hexdigest())
        self.held=HeldSourceFD(3,4,'quota.py',b'quota',('stamp',),self.pin)
        self.metadata=SimpleNamespace(st_dev=11,st_ino=22,st_mode=stat.S_IFREG|0o400)
        self.factory=lambda callback:None;self.dispatch=lambda broker:None
        for target in ['socket.socket','subprocess.Popen','subprocess.run','os.pidfd_open','ctypes.CDLL','threading.Thread.start']:
            q=patch(target,side_effect=AssertionError('runtime forbidden'));q.start();self.addCleanup(q.stop)
        q=patch('os.fstat',return_value=self.metadata);q.start();self.addCleanup(q.stop)
        q=patch('os.getuid',return_value=1000);q.start();self.addCleanup(q.stop)
    def broker(self):
        return wire.Broker._selected_quota_attempt('/owned',self.held,collector_factory=self.factory,
            dispatch_factory=self.dispatch,identity=lambda pid:KEY)
    def frame(self,role,seq=0):
        return dict(v=1,invocation_id='a'*32,parent_id=None,role=role,phase='begin' if seq==0 else 'decision' if seq==1 else 'terminal',
            seq=seq,decision='observation' if seq==1 else None,condition='metadata_only' if seq==1 else None,
            flags={},recipient_class='none',observer_ok=True)
    def test_actual_broker_internal_constructor_has_only_real_selected_pin(self):
        b=self.broker()
        self.assertEqual(b.ram.pins,{'quota_monitor':self.pin})
        self.assertEqual(b.ram.unselected,frozenset(ram.ROLES)-{'quota_monitor'})
        self.assertIs(b.factory,self.factory);self.assertIs(b.dispatch_factory,self.dispatch)
        self.assertIsNone(b.thread);self.assertIsNone(b.listener);self.assertIsNone(b.collector)
        self.assertEqual(b.ram.faults,{p:'dispatch_unqualified' for p in ram.PRODUCERS})
    def test_partial_snapshot_is_explicit_unselected_unknown_never_zero_aggregate(self):
        b=self.broker();answer=b.request({'v':1,'op':'snapshot','nonce':'b'*32},(321,1000,1000))
        self.assertFalse(answer['complete']);self.assertFalse(answer['ram_consistent'])
        self.assertEqual(answer['counts'],{p:None for p in ram.PRODUCERS})
        self.assertEqual(answer['source_selection'],{r:'SELECTED' if r=='quota_monitor' else 'UNSELECTED' for r in ram.ROLES})
        self.assertNotIn(None,b.ram.pins.values())
        for p in ('start_canary','backup'):self.assertIsNone(b.ram.counts[p])
    def test_all_unselected_roles_reject_admit_open_hook_exit_without_null_matching(self):
        for role in set(ram.ROLES)-{'quota_monitor'}:
            b=self.broker()
            for pin in (None,self.pin):
                with self.assertRaises(ram.Unknown):b.ram.admit('a'*32,role,None,KEY,pin)
                self.assertIsNone(b.ram.source_open(role,pin,KEY.pid,KEY))
            answer=b.request({'v':1,'op':'hook','frame':self.frame(role)},(321,1000,1000))
            self.assertIs(answer['ok'],False);self.assertEqual(b.ram.active,{})
            b.ram.process_exit('a'*32,KEY,0)
            self.assertEqual(b.ram.counts['quota_stale']['started'],0)
            self.assertEqual(b.ram.faults,{p:'dispatch_unqualified' for p in ram.PRODUCERS})
    def test_selected_role_math_uses_actual_same_core_and_cannot_heal_unknown(self):
        b=self.broker();state=b.ram
        state.admit('a'*32,'quota_monitor',None,KEY,self.pin)
        state.source_open('quota_monitor',self.pin,KEY.pid,KEY)
        for seq in range(3):self.assertIs(state.hook(self.frame('quota_monitor',seq),'a'*32,KEY)['ok'],True)
        state.process_exit('a'*32,KEY,0)
        self.assertEqual(state.counts['quota_stale'],{'started':1,'finished':1,'occurrences':0})
        answer=state.barrier('c'*32,lambda:True)
        self.assertFalse(answer['complete']);self.assertFalse(answer['ram_consistent'])
        self.assertEqual(answer['counts'],{p:None for p in ram.PRODUCERS})
        self.assertEqual(state.faults,{p:'dispatch_unqualified' for p in ram.PRODUCERS})
    def test_missing_null_or_forged_partial_selection_rejected_before_initialization(self):
        for selected in (None,self.pin,{'source_pin':self.pin}):
            with self.assertRaises((wire.Refused,ram.Unknown)):
                wire.Broker._selected_quota_attempt('/owned',selected,collector_factory=self.factory,dispatch_factory=self.dispatch)
        for pin in (None,{'dev':11,'ino':22,'sha256':self.pin.sha256}):
            with self.assertRaises(ram.Unknown):ram.LiveRAM._selected_quota_attempt(pin,wire.checked)
        self.metadata.st_ino=99
        with self.assertRaises(ram.Unknown):self.broker()
    def test_default_full_constructor_and_parser_still_reject_partial_input(self):
        partial={'quota_monitor':self.pin}
        with self.assertRaises(ram.Unknown):ram.LiveRAM(partial,wire.checked)
        with self.assertRaises(ram.Unknown):wire.Broker('/owned',partial)
        fields={'dev':11,'ino':22,'sha256':self.pin.sha256}
        cfg={'v':1,'enabled':True,'directory':'/owned','source_pins':{'quota_monitor':fields}}
        self.assertIsNone(wire.configured(cfg));self.assertIsNone(wire.start_optional({'directory':'/owned','source_pins':cfg['source_pins']}))
    def test_default_full_snapshot_shape_counters_and_firstfault_unchanged(self):
        full={r:ram.SourcePin(n,n+1,format(n,'064x')) for n,r in enumerate(ram.ROLES,1)}
        b=wire.Broker('/owned',full,identity=lambda pid:KEY)
        self.assertEqual(b.ram.unselected,frozenset());self.assertEqual(b.ram.pins,full)
        answer=b.ram.barrier('d'*32,lambda:True)
        self.assertNotIn('source_selection',answer)
        self.assertEqual(answer['counts'],{p:{'started':0,'finished':0,'occurrences':0} for p in ram.PRODUCERS})
        b.ram.poison(ram.PRODUCERS,'source_replaced')
        self.assertEqual(b.ram.faults,{p:'dispatch_unqualified' for p in ram.PRODUCERS})
    def test_partial_invalid_source_and_null_process_do_not_return_attribute_errors(self):
        state=self.broker().ram
        for process,pin in ((None,self.pin),(KEY,None)):
            with self.assertRaises(ram.Unknown):state.admit('a'*32,'quota_monitor',None,process,pin)
        self.assertIs(state.hook(self.frame('quota_monitor'),'a'*32,None)['ok'],False)


class ProducerTerminalProtocolTests(unittest.TestCase):
    """Actual quota main/check_health/check + real Client, fake effects only."""
    def setUp(self):
        import contextlib,copy,io,json
        from agent_hub_host import quota_dispatch_manager as mod
        self.mod=mod;self.json=json;self.clock=0.;self.sent=[];self.failures=[];self.probe_closes=[]
        self.frames=[];self.source_calls=0;self.alerts=0;self.writes=[]
        self.effects={'health':0,'quota':0};self.store={}
        root=Path(__file__).resolve().parents[3]
        def load(name,path):
            spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
        self.source=load('actual_selected_quota',root/'scripts/hub-quota-publish.py')
        self.client=load('actual_selected_client',root/'scripts/hub_alert_client.py')
        for target in ['socket.socket','subprocess.Popen','subprocess.run','os.pidfd_open','ctypes.CDLL','threading.Thread.start','os.open','urllib.request.urlopen']:
            q=patch(target,side_effect=AssertionError('runtime forbidden'));q.start();self.addCleanup(q.stop)
        q=patch.object(self.client.time,'monotonic',return_value=0.);q.start();self.addCleanup(q.stop)
        self.channel=SimpleNamespace(send=lambda raw:self.sent.append(raw) or len(raw),close=lambda:None)
        def pending(*a):raise BlockingIOError()
        self.channel.recvmsg=pending
        fm=FakeManager();fm.channel=self.channel;fm.lease.channel=self.channel;fm.lease.pidfd=4
        self.fm=fm;self.owner=QuotaDispatch(SimpleNamespace(ram=ram.LiveRAM._selected_quota_attempt(PIN,wire.checked,now=lambda:100)),fm,now=lambda:self.clock)
        self.ticket=self.owner.reserve(fm.source);self.owner.bind_started(self.ticket,self.channel);self.owner.source_open(self.ticket,object());self.owner.release(self.ticket)
        m=object.__new__(mod.V4QuotaManager);self.manager=m
        m.now=lambda:self.clock;m.p=SimpleNamespace(service_deadline=10.);m.phase='hook_receive';m.closed=False;m.ticket=self.ticket
        m.pending_channel=self.channel;m.frame_seq=0;m.frame_deadline=None;m.outbound=None;m.last_hook_ok=False;m.channel_eof=False
        m.collector=SimpleNamespace(fault=None);m.barriers=SimpleNamespace(close=lambda:None)
        m.probe=SimpleNamespace(close=lambda:self.probe_closes.append(1))
        m.fan=SimpleNamespace(drain=lambda *a:None);m.observation=SimpleNamespace(records=[{}],unknown=False);m.group=3
        m.lease=fm.lease;m.stable=SimpleNamespace(alive=lambda fd:True)
    def produce(self,*,health=False,critical=False,health_error=False,health_write_error=False,quota_write_error=False):
        import contextlib,copy,io
        source=self.source
        self.store={'health':{'version':1,'last_sample':0,'failures':1 if critical else 0,'alerted':False},
            'quota':{'version':1,'accounts':{name:100000 for name in source.ACCOUNTS},'alerted':[]}}
        @contextlib.contextmanager
        def locked(path):
            key='health' if str(path).endswith('health') else 'quota';self.effects[key]+=1;yield None
        def read_state(path,now):return copy.deepcopy(self.store['quota'])
        def private_json(path):return copy.deepcopy(self.store['health'])
        def write_state(path,value):
            key='health' if str(path).endswith('health') else 'quota';self.writes.append(key)
            if key=='health' and health_write_error:raise OSError('fixed fake state failure')
            if key=='quota' and quota_write_error:raise OSError('fixed fake state failure')
            self.store[key]=copy.deepcopy(value)
        def sample(args):
            if health_error:raise source.Failure('fixed_fake_health_error')
            return {'healthy':not critical,'stage':'ready','reason':'fixed'}
        def alert(*a,**k):self.alerts+=1;return True
        def exchange(path,request,*,deadline):
            self.assertEqual(set(request),{'v','op','frame'});self.assertEqual(request['op'],'hook')
            frame=wire.checked(request['frame']);self.frames.append(copy.deepcopy(frame))
            return {'seq':frame['seq'],'ok':True} # Fake ACK only, not admission.
        self.client.exchange=exchange
        observer=source.AlertObserver.__new__(source.AlertObserver)
        observer.child=self.client.Client(None,self.ticket.context,None,'quota_monitor');observer.dirty=False
        source.locked=locked;source.read_state=read_state;source.private_json=private_json
        source.write_state=write_state;source.sample_health=sample;source.alert=alert;source.now_ms=lambda:100000
        args=['--check','--state','/owned/quota','--health-state','/owned/health','--alert-command','/owned/sink','--alert-timeout','8']
        if health:args+=['--health-check','--health-config','/owned/config','--health-timeout','8']
        self.source_calls+=1
        with contextlib.redirect_stdout(io.StringIO()):result=source.main(args,_managed_observer=observer)
        return result
    def packet(self,frame):
        self.manager.packet=lambda deadline:self.json.dumps({'v':1,'op':'hook','frame':frame}).encode()
        self.manager.step(self.owner);self.assertEqual(self.manager.phase,'hook_send')
        self.manager.step(self.owner)
    def normal_source(self,health,expected):
        self.assertEqual(self.produce(health=health),0)
        self.assertEqual([f['phase'] for f in self.frames],['begin']+['decision']*(expected-2)+['terminal'])
        for frame in self.frames:self.packet(frame)
        self.assertEqual(self.manager.phase,'exit_poll');self.assertTrue(self.ticket.terminal)
        self.assertEqual(self.owner.ram.counts['quota_stale']['finished'],0)
        self.assertEqual(self.source_calls,1);self.assertEqual(self.effects['quota'],1)
    def test_real_nohealth_three_frames_reach_terminal_not_retirement(self):self.normal_source(False,3)
    def test_real_health_four_frames_reach_terminal_not_retirement(self):self.normal_source(True,4)
    def test_real_health_critical_then_quota_healthy_never_clears_first_occurrence_or_health_state(self):
        self.assertEqual(self.produce(health=True,critical=True),2)
        self.assertEqual([(f['decision'],f['condition']) for f in self.frames if f['phase']=='decision'],
            [('alert','health_critical'),('observation','quota_fresh')])
        for frame in self.frames:
            self.packet(frame)
            if frame['seq'] in (1,2):self.assertEqual(self.owner.ram.counts['quota_stale']['occurrences'],1)
        self.assertTrue(self.store['health']['alerted']);self.assertEqual(self.store['quota']['alerted'],[])
        self.assertEqual(self.alerts,1);self.assertEqual(self.manager.phase,'exit_poll')
        self.assertEqual(self.owner.ram.faults['quota_stale'],'dispatch_unqualified')
    def test_real_health_error_four_frames_remain_unknown_without_business_retry(self):
        self.assertEqual(self.produce(health=True,health_error=True),2);self.assertEqual(len(self.frames),4)
        self.assertEqual(self.frames[1]['condition'],'error');self.assertFalse(self.frames[1]['observer_ok'])
        self.packet(self.frames[0]);self.packet(self.frames[1])
        self.assertEqual(self.manager.phase,'failed');self.assertIsNotNone(self.ticket.fault)
        self.assertEqual(self.source_calls,1);self.assertEqual(self.effects,{'health':1,'quota':1})
    def test_actual_late_error_branches_prove_six_frame_upper_bound_not_magic_four(self):
        self.assertEqual(self.produce(health=True,health_write_error=True,quota_write_error=True),2)
        self.assertEqual(len(self.frames),6);self.assertEqual(self.mod.MAX_QUOTA_MONITOR_FRAMES,6)
        self.assertEqual([f['phase'] for f in self.frames],['begin','decision','decision','decision','decision','terminal'])
        self.assertEqual([f['seq'] for f in self.frames],list(range(6)));self.assertEqual(self.source_calls,1)
        self.assertFalse(self.frames[2]['observer_ok']);self.assertEqual(self.effects,{'health':1,'quota':1})
        self.assertEqual(self.alerts,1)
    def test_replay_and_early_terminal_get_negative_ack_and_sticky_failure(self):
        self.produce(health=True);self.packet(self.frames[0]);self.packet(self.frames[0])
        self.assertEqual(self.manager.phase,'failed');self.assertFalse(self.json.loads(self.sent[-1])['ok'])
        self.assertFalse(self.ticket.terminal);self.assertEqual(self.owner.ram.counts['quota_stale']['finished'],0)
    def test_early_terminal_without_decision_is_not_a_terminal_proof(self):
        self.produce();self.packet(self.frames[0]);early=dict(self.frames[-1],seq=1);self.packet(early)
        self.assertEqual(self.manager.phase,'failed');self.assertFalse(self.ticket.terminal)
    def test_wait_between_decisions_is_business_lifetime_but_ack_still_one150ms_budget(self):
        self.produce(health=True);self.packet(self.frames[0]);self.packet(self.frames[1]);self.clock=1.
        self.manager.packet=lambda deadline:None;self.manager.step(self.owner);self.assertEqual(self.manager.phase,'hook_receive')
        self.packet(self.frames[2]);self.assertEqual(self.manager.frame_deadline,1.15)
        self.manager.packet=lambda deadline:self.json.dumps({'v':1,'op':'hook','frame':self.frames[3]}).encode()
        self.manager.step(self.owner);self.clock=1.16;self.manager.step(self.owner)
        self.assertEqual(self.manager.phase,'failed');self.assertEqual(self.source_calls,1)
    def test_postterminal_packet_is_refused_while_pidfd_alive_and_eof_is_not_retirement(self):
        self.produce();
        for frame in self.frames:self.packet(frame)
        self.channel.recvmsg=lambda *args:(b'replay',[],0,None)
        self.manager.step(self.owner);self.assertEqual(self.manager.phase,'failed')
        self.assertEqual(self.owner.ram.counts['quota_stale']['finished'],0)
    def test_terminal_eof_yields_until_pidfd_exit_without_finished_credit(self):
        self.produce();
        for frame in self.frames:self.packet(frame)
        calls=[];self.channel.recvmsg=lambda *args:calls.append(args) or (b'',[],0,None)
        self.manager.step(self.owner);self.manager.step(self.owner)
        self.assertEqual(self.manager.phase,'exit_poll')
        self.assertEqual(len(calls),2);self.assertEqual(self.owner.ram.counts['quota_stale']['finished'],0)
    def test_absent_terminal_is_bounded_by_service_lifetime_not_an_unbounded_protocol(self):
        self.produce(health=True)
        for frame in self.frames[:-1]:self.packet(frame)
        self.clock=10.;self.manager.step(self.owner)
        self.assertEqual(self.manager.phase,'failed');self.assertFalse(self.ticket.terminal)
        self.assertEqual(self.source_calls,1)
    def test_source_derived_six_frame_cap_refuses_nonterminal_corruption_without_replay(self):
        self.produce();self.packet(self.frames[0])
        for seq in range(1,6):self.packet(dict(self.frames[1],seq=seq))
        self.assertEqual(self.manager.frame_seq,6);self.assertEqual(self.manager.phase,'failed')
        self.assertFalse(self.ticket.terminal);self.assertEqual(self.source_calls,1)
        self.assertEqual(self.owner.ram.counts['quota_stale']['finished'],0)
    def test_postterminal_control_truncation_is_unknown_not_closure(self):
        import socket
        self.produce()
        for frame in self.frames:self.packet(frame)
        self.channel.recvmsg=lambda *args:(b'',[],socket.MSG_CTRUNC,None)
        self.manager.step(self.owner);self.assertEqual(self.manager.phase,'failed')
        self.assertEqual(self.source_calls,1);self.assertEqual(self.owner.ram.counts['quota_stale']['finished'],0)


class SameProcessLifecycleTests(unittest.TestCase):
    """End-to-end same-process boundary, OS-adapter, and failure-seam tests."""

    def _short_temp(self):
        import tempfile
        base = os.environ.get('RUNNER_TEMP')
        if not base:
            xdg = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
            if os.path.isdir(xdg) and os.access(xdg, os.W_OK):
                base = xdg
        td = tempfile.TemporaryDirectory(prefix='qd-', dir=base)
        self.addCleanup(td.cleanup)
        return Path(td.name)

    def _write_readonly(self, directory, name, content):
        path = directory / name
        path.write_bytes(content)
        path.chmod(0o400)
        return path

    def test_opt_in_default_off_has_zero_io(self):
        from agent_hub_host.quota_dispatch import configured_same_process, run_same_process_quota
        with patch('socket.socket', side_effect=AssertionError('socket forbidden')), \
             patch('subprocess.run', side_effect=AssertionError('subprocess forbidden')):
            res = run_same_process_quota({'proof': True}, lambda t: 0)
            self.assertEqual(res, {'enabled': False, 'dispatched': False, 'authority': 'NONE', 'complete': False})
            self.assertIsNone(configured_same_process(None))
            self.assertIsNone(configured_same_process({'v': 1, 'enabled': False}))

    def test_cli_quota_dispatch_default_off_exits_zero_without_io(self):
        import contextlib, io, json
        from agent_hub_host import cli
        out = io.StringIO()
        with patch('socket.socket', side_effect=AssertionError('socket forbidden')), \
             patch('subprocess.run', side_effect=AssertionError('subprocess forbidden')), \
             contextlib.redirect_stdout(out):
            rc = cli.main(['--config', '/nonexistent/v2.json', 'quota-dispatch'])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue()), {'enabled': False, 'dispatched': False, 'authority': 'NONE', 'complete': False})

    def test_negative_client_self_admission_and_proof_dict_rejected(self):
        from agent_hub_host.quota_dispatch import run_same_process_quota
        with self.assertRaises(ram.Unknown):
            run_same_process_quota({'proof': True}, lambda t: 0, enabled=True, broker_directory='/unused')

    def test_hold_source_fd_and_changed_source_refused(self):
        from agent_hub_host.quota_dispatch_manager import StableRuntime, close_held_source, hold_source_fd
        root = self._short_temp()
        src_dir = root / 'src'
        src_dir.mkdir(mode=0o700)
        target = self._write_readonly(src_dir, 'quota.py', b'print("ok")\n')
        held = hold_source_fd(src_dir, 'quota.py')
        self.addCleanup(close_held_source, held)
        stable = StableRuntime()
        stable.checked_source(held.fd, held.directory_fd, held.name, held.raw, held.stamp)
        target.chmod(0o600)
        target.write_bytes(b'print("tampered")\n')
        target.chmod(0o400)
        with self.assertRaises(ram.Unknown):
            stable.checked_source(held.fd, held.directory_fd, held.name, held.raw, held.stamp)

    def test_stable_inspect_exec_and_process_and_user_manager_env(self):
        from agent_hub_host.quota_dispatch_manager import StableRuntime, UserManagerEnvironment
        stable = StableRuntime()
        stable.inspect_exec(
            b'ExecStart={ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 /opt/b.py 1 2 3 ; ignore_errors=no }\n',
            '/usr/bin/python3 /opt/b.py 1 2 3',
        )
        with self.assertRaises(ram.Unknown):
            stable.inspect_exec(b'ExecStart=/usr/bin/false\n', '/usr/bin/python3 /opt/b.py 1 2 3')
        pid = os.getpid()
        cgroup = next(
            line.split(':', 2)[-1]
            for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines()
            if ':' in line
        )
        key = stable.inspect_process(pid, cgroup)
        self.assertEqual(key[0], pid)
        with self.assertRaises(ram.Unknown):
            stable.inspect_process(pid, '/wrong/cgroup.slice')
        plan = {'units': {'healthy': 'u.service'}, 'cgroups': {'healthy': cgroup}}
        runner = SimpleNamespace(run=lambda argv, timeout: (
            0, f'MainPID={pid}\nControlGroup={cgroup}\nInvocationID={"a"*32}\n'.encode()
        ))
        env = UserManagerEnvironment()
        state = env.show(runner, plan, 'healthy', env.now() + 5)
        self.assertEqual(env.owned(state, plan, 'healthy'), 'a' * 32)
        with self.assertRaises(ram.Unknown):
            env.owned(state, plan, 'healthy', 'b' * 32)

    def test_fanotify_reader_ordinary_read_and_overflow_loss(self):
        import struct
        from agent_hub_host.quota_dispatch_manager import FanotifyObservation, FanotifyReader
        r_pipe, w_pipe = os.pipe()
        self.addCleanup(lambda: [os.close(fd) for fd in (r_pipe, w_pipe) if fd is not None])
        os.set_blocking(r_pipe, False)
        reader = FanotifyReader()
        obs = FanotifyObservation()
        ev_fd = os.open(__file__, os.O_RDONLY)
        pkt = FanotifyReader._STRUCT.pack(24, 3, 0, 24, FanotifyReader._FAN_OPEN, ev_fd, 99999)
        os.write(w_pipe, pkt)
        reader.drain(r_pipe, obs)
        self.assertFalse(obs.unknown)
        self.assertEqual(len(obs.records), 1)
        self.assertEqual(obs.records[0]['pid'], 99999)
        # Overflow sets unknown=True cleanly
        pkt_ovf = FanotifyReader._STRUCT.pack(24, 3, 0, 24, FanotifyReader._FAN_Q_OVERFLOW, -1, 0)
        os.write(w_pipe, pkt_ovf)
        reader.drain(r_pipe, obs)
        self.assertTrue(obs.unknown)
        self.assertEqual(obs.records, [])

    def test_same_process_full_lifecycle_reservation_precedes_launch_and_negative_public_hook(self):
        import json, socket, threading
        from agent_hub_host.quota_dispatch import run_same_process_quota
        from agent_hub_host.quota_dispatch_manager import (
            FanotifyObservation, SourceGate, close_held_source, hold_source_fd, prepare_selected_quota,
        )
        root = self._short_temp()
        broker_dir = root / 'broker'
        gate_dir = root / 'gate'
        src_dir = root / 'src'
        for d in (broker_dir, gate_dir, src_dir):
            d.mkdir(mode=0o700)
        self._write_readonly(src_dir, 'quota.py', b'print("q")\n')
        self._write_readonly(src_dir, 'client.py', b'print("c")\n')
        self._write_readonly(src_dir, 'boot.py', b'print("b")\n')
        q_fd = hold_source_fd(src_dir, 'quota.py')
        c_fd = hold_source_fd(src_dir, 'client.py')
        b_fd = hold_source_fd(src_dir, 'boot.py')
        gate = SourceGate(gate_dir)

        r_fan, w_fan = os.pipe()
        os.set_blocking(r_fan, False)
        obs = FanotifyObservation()

        class FakeFan:
            def drain(self, group, o):
                o.records = []
                o.unknown = False
                try:
                    raw = os.read(group, 64)
                    if raw == b'open':
                        o.records = [{'pid': os.getpid(), 'dev': q_fd.pin.dev, 'ino': q_fd.pin.ino, 'mask': 0x20}]
                except BlockingIOError:
                    pass

        class FakeCollector:
            def __init__(self, cb):
                self.cb = cb
                self.wire = SimpleNamespace(receive=lambda *a: False, send=lambda *a: 1)
                self.epoch = 'e' * 32
                self.fault = None
                self.manager = ':1.9'
                self.jobs = {}
                self.started = True
                self.pending = None
            def start(self):
                self.cb({'member': 'JobNew', 'job_id': 1, 'unit': 'u.service', 'result': None, 'stream_index': 3, 'manager_serial': 10, 'epoch': self.epoch})
                return {'stream_index': 1}
            def handle(self, *a):
                pass
            def receipt(self, idx):
                return {'stream_index': idx}
            def barrier(self):
                return {'stream_index': 10}
            def close(self):
                pass

        alive_flag = {'alive': True}
        stable = SimpleNamespace(
            UID=os.getuid(),
            source_stamp=lambda info: q_fd.stamp if info.st_ino == q_fd.pin.ino else (c_fd.stamp if info.st_ino == c_fd.pin.ino else b_fd.stamp),
            checked_source=lambda *a: None,
            inspect_exec=lambda *a: None,
            inspect_process=lambda pid, cg: (os.getpid(), 77, '11111111-2222-4333-8444-555555555555'),
            alive=lambda pidfd: alive_flag['alive'],
        )
        env = SimpleNamespace(
            remaining=lambda d, c=1: 1.0,
            show=lambda r, p, role, d: {'MainPID': str(os.getpid()) if alive_flag['alive'] else '0', 'ControlGroup': '/u.slice', 'InvocationID': 'a' * 32},
            owned=lambda s, p, role, inv=None: 'a' * 32,
        )
        runner = SimpleNamespace(run=lambda argv, t: (0, b'ExecMainCode=1\nExecMainStatus=0\n' if 'ExecMainCode' in argv[-1] else b'ExecStart=/usr/bin/python3\n'))
        plan = {'units': {'healthy': 'u.service'}, 'cgroups': {'healthy': '/u.slice'}}
        prepared = prepare_selected_quota(
            source=q_fd, companions=(c_fd, b_fd), gate=gate, group=r_fan,
            plan=plan, exec_argv=lambda ns: ['/usr/bin/python3'],
            service_deadline=time.monotonic() + 5.0,
            stable=stable, manager_environment=env, fan=FakeFan(), observation=obs, runner=runner,
            extra_cleanup=lambda: os.close(w_fan),
        )

        order = []
        child_err = []

        def _fake_launch(ticket):
            order.append(('launch', ticket is not None))
            self.assertIsNotNone(ticket)
            self.assertTrue(ram.identity(ticket.context))

            def _child():
                sock = None
                try:
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
                    sock.connect(str(gate_dir / 'source-gate.sock'))
                    sock.send(b'held-before-quota-source')
                    cmd = sock.recv(256)
                    self.assertEqual(cmd, b'open:' + ticket.context.encode('ascii'))
                    os.write(w_fan, b'open')
                    sock.send(b'quota-source-opened')
                    rel = sock.recv(256)
                    self.assertEqual(rel, b'qualified-run')
                    # Negative client self-admission on public observer.sock must be refused!
                    pub = wire.exchange(broker_dir / 'observer.sock', {
                        'v': 1, 'op': 'hook', 'frame': {
                            'v': 1, 'invocation_id': ticket.context, 'parent_id': None,
                            'role': 'quota_monitor', 'phase': 'begin', 'seq': 0,
                            'decision': None, 'condition': None, 'flags': {},
                            'recipient_class': 'none', 'observer_ok': True,
                        },
                    })
                    self.assertIs(pub['ok'], False)
                    alive_flag['alive'] = False
                except BaseException as exc:
                    child_err.append(exc)
                finally:
                    if sock is not None:
                        sock.close()

            t = threading.Thread(target=_child, daemon=True)
            t.start()
            t.join(timeout=2.0)
            return 0

        class InlineBarrierCollector(FakeCollector):
            pass

        with patch('os.pidfd_open', return_value=os.dup(r_fan)):
            # Drive OrderedBarrier + events minimally by patching OrderedBarrier.begin/poll on this test
            with patch('agent_hub_host.quota_dispatch_manager.OrderedBarrier.begin', lambda self, d: None), \
                 patch('agent_hub_host.quota_dispatch_manager.OrderedBarrier.poll', lambda self: {'stream_index': 2}):
                res = run_same_process_quota(
                    prepared, _fake_launch, enabled=True, broker_directory=broker_dir,
                    collector_factory=InlineBarrierCollector,
                    identity=lambda pid: KEY,
                )
        self.assertEqual(child_err, [])
        self.assertEqual(order, [('launch', True)])
        self.assertTrue(res['enabled'])
        self.assertTrue(res['dispatched'])
        self.assertTrue(res['reserved'])
        self.assertFalse(res['complete'])
        # Because public_hook was attempted, sticky failure is preserved!
        self.assertEqual(res['phase'], 'failed')

    def test_early_crash_and_dropped_hook_preserve_once_only_business_and_sticky_fault(self):
        from agent_hub_host.quota_dispatch import run_same_process_quota
        from agent_hub_host.quota_dispatch_manager import (
            FanotifyObservation, SourceGate, hold_source_fd, prepare_selected_quota,
        )
        root = self._short_temp()
        broker_dir = root / 'broker'
        gate_dir = root / 'gate'
        src_dir = root / 'src'
        for d in (broker_dir, gate_dir, src_dir):
            d.mkdir(mode=0o700)
        self._write_readonly(src_dir, 'quota.py', b'print("q")\n')
        self._write_readonly(src_dir, 'client.py', b'print("c")\n')
        self._write_readonly(src_dir, 'boot.py', b'print("b")\n')
        q_fd = hold_source_fd(src_dir, 'quota.py')
        c_fd = hold_source_fd(src_dir, 'client.py')
        b_fd = hold_source_fd(src_dir, 'boot.py')
        gate = SourceGate(gate_dir)
        r_fan, w_fan = os.pipe()
        os.set_blocking(r_fan, False)

        class FakeCollector:
            def __init__(self, cb):
                self.wire = SimpleNamespace(receive=lambda *a: False)
                self.epoch = 'e' * 32
                self.fault = None
                self.started = True
                self.pending = None
                self.jobs = {}
            def start(self):
                self.cb({'member': 'JobNew', 'job_id': 1, 'unit': 'u.service', 'result': None, 'stream_index': 3, 'manager_serial': 10, 'epoch': self.epoch})
                return {'stream_index': 1}
            def handle(self, *a):
                pass
            def barrier(self):
                return {'stream_index': 2}
            def close(self):
                pass

        prepared = prepare_selected_quota(
            source=q_fd, companions=(c_fd, b_fd), gate=gate, group=r_fan,
            plan={'units': {'healthy': 'u.service'}, 'cgroups': {'healthy': '/u.slice'}},
            exec_argv=lambda ns: ['/usr/bin/python3'],
            service_deadline=time.monotonic() + 0.25,
            fan=SimpleNamespace(drain=lambda g, o: None),
            observation=FanotifyObservation(),
            extra_cleanup=lambda: os.close(w_fan),
        )
        launches = []
        with patch('agent_hub_host.quota_dispatch_manager.OrderedBarrier.begin', lambda self, d: None), \
             patch('agent_hub_host.quota_dispatch_manager.OrderedBarrier.poll', lambda self: {'stream_index': 2}):
            res = run_same_process_quota(
                prepared, lambda ticket: launches.append(ticket) or 73,
                enabled=True, broker_directory=broker_dir,
                collector_factory=FakeCollector, identity=lambda pid: KEY,
            )
        self.assertEqual(len(launches), 1)
        self.assertEqual(res['launch_result'], 73)
        self.assertEqual(res['phase'], 'failed')
        self.assertFalse(res['complete'])

    def test_run_same_process_quota_broker_failure_runs_launcher_once_and_no_double_launch(self):
        from agent_hub_host.quota_dispatch import run_same_process_quota
        from agent_hub_host.quota_dispatch_manager import (
            FanotifyObservation, SourceGate, hold_source_fd, prepare_selected_quota,
        )
        root = self._short_temp()
        broker_dir = root / 'broker'
        gate_dir = root / 'gate'
        src_dir = root / 'src'
        for d in (broker_dir, gate_dir, src_dir):
            d.mkdir(mode=0o700)
        self._write_readonly(src_dir, 'quota.py', b'print("q")\n')
        self._write_readonly(src_dir, 'client.py', b'print("c")\n')
        self._write_readonly(src_dir, 'boot.py', b'print("b")\n')
        q_fd = hold_source_fd(src_dir, 'quota.py')
        c_fd = hold_source_fd(src_dir, 'client.py')
        b_fd = hold_source_fd(src_dir, 'boot.py')
        gate = SourceGate(gate_dir)
        r_fan, w_fan = os.pipe()
        os.set_blocking(r_fan, False)
        prepared = prepare_selected_quota(
            source=q_fd, companions=(c_fd, b_fd), gate=gate, group=r_fan,
            plan={'units': {'healthy': 'u.service'}, 'cgroups': {'healthy': '/u.slice'}},
            exec_argv=lambda ns: ['/usr/bin/python3'],
            service_deadline=time.monotonic() + 2.0,
            fan=SimpleNamespace(drain=lambda g, o: None),
            observation=FanotifyObservation(),
            extra_cleanup=lambda: os.close(w_fan),
        )
        # Broker failure runs launcher(None) once AND cleans up owned gate/FDs
        cleaned = []
        orig_cleanup = prepared.cleanup_owned
        object.__setattr__(prepared, 'cleanup_owned', lambda: (cleaned.append(1), orig_cleanup())[1])
        launches = []
        with patch.object(wire.Broker, '_selected_quota_attempt', side_effect=OSError('broker_init_failed')):
            res = run_same_process_quota(
                prepared, lambda t: launches.append(t) or 42,
                enabled=True, broker_directory=broker_dir,
            )
        self.assertEqual(launches, [None])
        self.assertEqual(cleaned, [1])
        self.assertEqual(res['launch_result'], 42)
        self.assertFalse(res['dispatched'])
        self.assertFalse(res['complete'])

    def test_run_same_process_quota_preserves_original_primary_exception_identity(self):
        from agent_hub_host.quota_dispatch import run_same_process_quota
        from agent_hub_host.quota_dispatch_manager import (
            FanotifyObservation, SourceGate, hold_source_fd, prepare_selected_quota,
        )
        root = self._short_temp()
        broker_dir = root / 'broker'
        gate_dir = root / 'gate'
        src_dir = root / 'src'
        for d in (broker_dir, gate_dir, src_dir):
            d.mkdir(mode=0o700)
        self._write_readonly(src_dir, 'quota.py', b'print("q")\n')
        self._write_readonly(src_dir, 'client.py', b'print("c")\n')
        self._write_readonly(src_dir, 'boot.py', b'print("b")\n')
        q_fd = hold_source_fd(src_dir, 'quota.py')
        c_fd = hold_source_fd(src_dir, 'client.py')
        b_fd = hold_source_fd(src_dir, 'boot.py')
        gate = SourceGate(gate_dir)
        r_fan, w_fan = os.pipe()
        os.set_blocking(r_fan, False)
        prepared = prepare_selected_quota(
            source=q_fd, companions=(c_fd, b_fd), gate=gate, group=r_fan,
            plan={'units': {'healthy': 'u.service'}, 'cgroups': {'healthy': '/u.slice'}},
            exec_argv=lambda ns: ['/usr/bin/python3'],
            service_deadline=time.monotonic() + 2.0,
            fan=SimpleNamespace(drain=lambda g, o: None),
            observation=FanotifyObservation(),
            extra_cleanup=lambda: os.close(w_fan),
        )

        class SentinelPrimaryError(Exception):
            pass

        sentinel = SentinelPrimaryError('original_primary')
        launches = []

        def _boom(t):
            launches.append(t)
            raise sentinel

        with self.assertRaises(SentinelPrimaryError) as caught:
            run_same_process_quota(
                prepared, _boom, enabled=True, broker_directory=broker_dir,
                collector_factory=lambda cb: SimpleNamespace(
                    wire=SimpleNamespace(receive=lambda *a: False), epoch='e'*32,
                    fault=None, started=True, pending=None, jobs={},
                    start=lambda: {'stream_index': 1}, handle=lambda *a: None,
                    barrier=lambda: {'stream_index': 2}, close=lambda: None,
                ),
                identity=lambda pid: KEY,
            )
        self.assertIs(caught.exception, sentinel)
        self.assertEqual(len(launches), 1)

    def test_run_same_process_quota_cleanup_base_exception_never_masks_primary_sentinel(self):
        from agent_hub_host.quota_dispatch import run_same_process_quota
        from agent_hub_host.quota_dispatch_manager import (
            FanotifyObservation, SourceGate, hold_source_fd, prepare_selected_quota,
        )
        root = self._short_temp()
        broker_dir = root / 'broker'
        gate_dir = root / 'gate'
        src_dir = root / 'src'
        for d in (broker_dir, gate_dir, src_dir):
            d.mkdir(mode=0o700)
        self._write_readonly(src_dir, 'quota.py', b'print("q")\n')
        self._write_readonly(src_dir, 'client.py', b'print("c")\n')
        self._write_readonly(src_dir, 'boot.py', b'print("b")\n')
        q_fd = hold_source_fd(src_dir, 'quota.py')
        c_fd = hold_source_fd(src_dir, 'client.py')
        b_fd = hold_source_fd(src_dir, 'boot.py')
        gate = SourceGate(gate_dir)
        r_fan, w_fan = os.pipe()
        os.set_blocking(r_fan, False)
        prepared = prepare_selected_quota(
            source=q_fd, companions=(c_fd, b_fd), gate=gate, group=r_fan,
            plan={'units': {'healthy': 'u.service'}, 'cgroups': {'healthy': '/u.slice'}},
            exec_argv=lambda ns: ['/usr/bin/python3'],
            service_deadline=time.monotonic() + 2.0,
            fan=SimpleNamespace(drain=lambda g, o: None),
            observation=FanotifyObservation(),
            extra_cleanup=lambda: os.close(w_fan),
        )
        orig_cleanup = prepared.cleanup_owned
        def _bad_cleanup():
            orig_cleanup()
            raise KeyboardInterrupt('cleanup_interrupt')
        object.__setattr__(prepared, 'cleanup_owned', _bad_cleanup)

        class SentinelPrimaryError(Exception):
            pass

        sentinel = SentinelPrimaryError('must_propagate')
        with patch.object(wire.Broker, '_selected_quota_attempt', side_effect=OSError('broker_fail')):
            with self.assertRaises(SentinelPrimaryError) as caught:
                run_same_process_quota(
                    prepared, lambda t: (_ for _ in ()).throw(sentinel),
                    enabled=True, broker_directory=broker_dir,
                )
        self.assertIs(caught.exception, sentinel)

    def test_run_same_process_quota_paused_collector_never_prematurely_closes_borrowed_fds(self):
        import threading
        from agent_hub_host.quota_dispatch import run_same_process_quota
        from agent_hub_host.quota_dispatch_manager import (
            FanotifyObservation, SourceGate, hold_source_fd, prepare_selected_quota,
        )
        root = self._short_temp()
        broker_dir = root / 'broker'
        gate_dir = root / 'gate'
        src_dir = root / 'src'
        for d in (broker_dir, gate_dir, src_dir):
            d.mkdir(mode=0o700)
        self._write_readonly(src_dir, 'quota.py', b'print("q")\n')
        self._write_readonly(src_dir, 'client.py', b'print("c")\n')
        self._write_readonly(src_dir, 'boot.py', b'print("b")\n')
        q_fd = hold_source_fd(src_dir, 'quota.py')
        c_fd = hold_source_fd(src_dir, 'client.py')
        b_fd = hold_source_fd(src_dir, 'boot.py')
        gate = SourceGate(gate_dir)
        r_fan, w_fan = os.pipe()
        os.set_blocking(r_fan, False)
        prepared = prepare_selected_quota(
            source=q_fd, companions=(c_fd, b_fd), gate=gate, group=r_fan,
            plan={'units': {'healthy': 'u.service'}, 'cgroups': {'healthy': '/u.slice'}},
            exec_argv=lambda ns: ['/usr/bin/python3'],
            service_deadline=time.monotonic() + 0.05,
            fan=SimpleNamespace(drain=lambda g, o: None),
            observation=FanotifyObservation(),
            extra_cleanup=lambda: os.close(w_fan),
        )
        cleaned = []
        orig_cleanup = prepared.cleanup_owned
        object.__setattr__(prepared, 'cleanup_owned', lambda: (cleaned.append(os.fstat(q_fd.fd).st_ino), orig_cleanup())[1])

        release_collector = threading.Event()
        entered_collector = threading.Event()
        class SlowCollector:
            def __init__(self, cb):
                self.wire = SimpleNamespace(receive=lambda *a: False)
            def start(self):
                entered_collector.set()
                release_collector.wait(2.0)
                return {'stream_index': 1}
            def handle(self, *a):
                pass
            def barrier(self):
                pass
            def close(self):
                pass

        res = run_same_process_quota(
            prepared, lambda t: 55, enabled=True, broker_directory=broker_dir,
            collector_factory=SlowCollector, identity=lambda pid: KEY,
        )
        self.assertTrue(entered_collector.is_set())
        self.assertEqual(res['launch_result'], 55)
        # Owner thread is still alive past 0.2s close join -> must NOT have prematurely closed borrowed FDs
        self.assertEqual(cleaned, [])
        release_collector.set()
        deadline = time.monotonic() + 2.0
        while not cleaned and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(cleaned), 1)

    def test_run_same_process_quota_late_collector_exception_cleans_up_once_after_owner_exit(self):
        import threading
        from agent_hub_host.quota_dispatch import run_same_process_quota
        from agent_hub_host.quota_dispatch_manager import (
            FanotifyObservation, SourceGate, hold_source_fd, prepare_selected_quota,
        )
        root = self._short_temp()
        broker_dir = root / 'broker'
        gate_dir = root / 'gate'
        src_dir = root / 'src'
        for d in (broker_dir, gate_dir, src_dir):
            d.mkdir(mode=0o700)
        self._write_readonly(src_dir, 'quota.py', b'print("q")\n')
        self._write_readonly(src_dir, 'client.py', b'print("c")\n')
        self._write_readonly(src_dir, 'boot.py', b'print("b")\n')
        q_fd = hold_source_fd(src_dir, 'quota.py')
        c_fd = hold_source_fd(src_dir, 'client.py')
        b_fd = hold_source_fd(src_dir, 'boot.py')
        gate = SourceGate(gate_dir)
        r_fan, w_fan = os.pipe()
        os.set_blocking(r_fan, False)
        prepared = prepare_selected_quota(
            source=q_fd, companions=(c_fd, b_fd), gate=gate, group=r_fan,
            plan={'units': {'healthy': 'u.service'}, 'cgroups': {'healthy': '/u.slice'}},
            exec_argv=lambda ns: ['/usr/bin/python3'],
            service_deadline=time.monotonic() + 0.05,
            fan=SimpleNamespace(drain=lambda g, o: None),
            observation=FanotifyObservation(),
            extra_cleanup=lambda: os.close(w_fan),
        )
        cleaned = []
        orig_cleanup = prepared.cleanup_owned
        object.__setattr__(prepared, 'cleanup_owned', lambda: (cleaned.append(os.fstat(q_fd.fd).st_ino), orig_cleanup())[1])

        release_collector = threading.Event()
        entered_collector = threading.Event()
        class LateThrowCollector:
            def __init__(self, cb):
                self.wire = SimpleNamespace(receive=lambda *a: False)
            def start(self):
                entered_collector.set()
                release_collector.wait(2.0)
                raise RuntimeError('late_collector_boom')
            def handle(self, *a):
                pass
            def barrier(self):
                pass
            def close(self):
                pass

        res = run_same_process_quota(
            prepared, lambda t: 66, enabled=True, broker_directory=broker_dir,
            collector_factory=LateThrowCollector, identity=lambda pid: KEY,
        )
        self.assertTrue(entered_collector.is_set())
        self.assertEqual(res['launch_result'], 66)
        self.assertFalse(res['reserved'])
        self.assertEqual(res['phase'], 'failed')
        self.assertFalse(res['complete'])
        self.assertEqual(cleaned, [])
        release_collector.set()
        deadline = time.monotonic() + 2.0
        while not cleaned and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(cleaned), 1)
        with self.assertRaises(OSError):
            os.fstat(q_fd.fd)

    def test_real_subprocess_prelude_retained_fd_transport_on_swapped_path(self):
        import hashlib, subprocess, sys
        root = self._short_temp()
        src_dir = root / 'src'
        src_dir.mkdir(mode=0o700)
        orig_bytes = b'def main(argv=None, *, _managed_observer=None):\n    return 19 if _managed_observer is None else 99\n'
        q_path = self._write_readonly(src_dir, 'hub-quota-publish.py', orig_bytes)
        c_path = self._write_readonly(src_dir, 'hub_alert_client.py', b'print("c")\n')
        q_sha = hashlib.sha256(orig_bytes).hexdigest()
        c_sha = hashlib.sha256(b'print("c")\n').hexdigest()
        retained_fd = os.open(str(q_path), os.O_RDONLY | os.O_NOFOLLOW)
        self.addCleanup(lambda: os.close(retained_fd))
        os.set_inheritable(retained_fd, True)
        # Swap path on disk after parent selection
        q_path.unlink()
        self._write_readonly(src_dir, 'hub-quota-publish.py', b'raise AssertionError("replacement_executed")\n')
        repo_root = Path(__file__).resolve().parents[3]
        prelude = repo_root / 'scripts/hub-alert-quota-dispatch.py'
        proc = subprocess.run(
            [sys.executable, '-I', '-S', str(prelude), '--retained-fd', str(retained_fd),
             '0', q_sha, c_sha, str(root), str(q_path), str(c_path), '--', '--check'],
            pass_fds=(retained_fd,), capture_output=True, timeout=5, check=False,
        )
        self.assertEqual(proc.returncode, 19)

    def test_root_preparer_inert_default_off_and_handoff_privilege_drop_boundary(self):
        import contextlib, io, json, shutil, sys
        repo_root = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location('root_preparer', repo_root / 'scripts/hub-alert-quota-prepare.py')
        prep = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(prep)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(prep.main([]), 0)
        self.assertEqual(json.loads(out.getvalue())['status'], 'INERT_ROOT_PREPARER')
        with self.assertRaises(PermissionError):
            prep.drop_privileges(0, 0)
        # Full interpreter binary pin without 262144-byte cap
        real_py = os.path.realpath(sys.executable)
        interp_fd, interp_pin = prep.pin_interpreter(real_py)
        os.close(interp_fd)
        self.assertGreater(interp_pin['size'], 262144)
        self.assertEqual(len(interp_pin['sha256']), 64)
        # Staged handoff passes inherited FDs + read-only pipe manifest to runner after privilege drop
        root = self._short_temp()
        gate_dir = root / 'gate'
        src_dir = root / 'src'
        gate_dir.mkdir(mode=0o700)
        src_dir.mkdir(mode=0o700)
        self._write_readonly(src_dir, 'hub-quota-publish.py', b'print("q")\n')
        shutil.copyfile(repo_root / 'scripts/hub_alert_client.py', src_dir / 'hub_alert_client.py')
        shutil.copyfile(repo_root / 'scripts/hub-alert-quota-dispatch.py', src_dir / 'hub-alert-quota-dispatch.py')
        (src_dir / 'hub_alert_client.py').chmod(0o400)
        (src_dir / 'hub-alert-quota-dispatch.py').chmod(0o400)
        r_fan, w_fan = os.pipe()
        os.close(w_fan)
        seen = {}
        def _fake_runner(cmd, *, pass_fds, env, check):
            idx = cmd.index('--handoff-fd')
            pipe_fd = int(cmd[idx + 1])
            seen['cmd'] = cmd
            seen['pass_fds'] = pass_fds
            seen['manifest'] = json.loads(os.read(pipe_fd, 65536).decode('utf-8'))
            return SimpleNamespace(returncode=0)
        args = SimpleNamespace(
            target_uid=os.getuid(), config=str(root / 'v2.json'),
            source_dir=str(src_dir), gate_dir=str(gate_dir), python_bin=real_py,
        )
        with patch.object(prep, 'open_fanotify_group', return_value=r_fan):
            rc = prep.prepare_and_handoff(args, runner=_fake_runner)
        self.assertEqual(rc, 0)
        self.assertIn(seen['manifest']['fan_group'], seen['pass_fds'])
        self.assertEqual(seen['manifest']['interpreter']['sha256'], interp_pin['sha256'])

    def test_fanotify_reader_bad_version_header_closes_trailing_valid_fds(self):
        from agent_hub_host.quota_dispatch_manager import FanotifyObservation, FanotifyReader
        r_pipe, w_pipe = os.pipe()
        self.addCleanup(lambda: [os.close(fd) for fd in (r_pipe, w_pipe)])
        os.set_blocking(r_pipe, False)
        reader = FanotifyReader()
        obs = FanotifyObservation()
        ev1 = os.open(__file__, os.O_RDONLY)
        ev2 = os.open(__file__, os.O_RDONLY)
        pkt_bad_vers = FanotifyReader._STRUCT.pack(24, 99, 0, 24, FanotifyReader._FAN_OPEN, -1, 111)
        pkt1 = FanotifyReader._STRUCT.pack(24, 3, 0, 24, FanotifyReader._FAN_OPEN, ev1, 222)
        pkt2 = FanotifyReader._STRUCT.pack(24, 3, 0, 24, FanotifyReader._FAN_OPEN, ev2, 333)
        os.write(w_pipe, pkt_bad_vers + pkt1 + pkt2)
        reader.drain(r_pipe, obs)
        self.assertTrue(obs.unknown)
        self.assertEqual(obs.records, [])
        for fd in (ev1, ev2):
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_fanotify_reader_bounded_drain_and_closes_trailing_fds_on_loss(self):
        from agent_hub_host.quota_dispatch_manager import FanotifyObservation, FanotifyReader
        r_pipe, w_pipe = os.pipe()
        self.addCleanup(lambda: [os.close(fd) for fd in (r_pipe, w_pipe)])
        os.set_blocking(r_pipe, False)
        reader = FanotifyReader()
        obs = FanotifyObservation()
        ev1 = os.open(__file__, os.O_RDONLY)
        ev2 = os.open(__file__, os.O_RDONLY)
        pkt_ovf = FanotifyReader._STRUCT.pack(24, 3, 0, 24, FanotifyReader._FAN_Q_OVERFLOW, -1, 0)
        pkt1 = FanotifyReader._STRUCT.pack(24, 3, 0, 24, FanotifyReader._FAN_OPEN, ev1, 111)
        pkt2 = FanotifyReader._STRUCT.pack(24, 3, 0, 24, FanotifyReader._FAN_OPEN, ev2, 222)
        os.write(w_pipe, pkt_ovf + pkt1 + pkt2)
        reader.drain(r_pipe, obs)
        self.assertTrue(obs.unknown)
        self.assertEqual(obs.records, [])
        for fd in (ev1, ev2):
            with self.assertRaises(OSError):
                os.fstat(fd)

        # Bounded by MAX_RECORDS (fresh observation)
        obs2 = FanotifyObservation()
        fds = [os.open(__file__, os.O_RDONLY) for _ in range(FanotifyReader.MAX_RECORDS + 2)]
        buf = b''.join(FanotifyReader._STRUCT.pack(24, 3, 0, 24, FanotifyReader._FAN_OPEN, fd, 333) for fd in fds)
        os.write(w_pipe, buf)
        reader.drain(r_pipe, obs2)
        self.assertTrue(obs2.unknown)
        self.assertEqual(obs2.records, [])
        for fd in fds:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_cli_quota_dispatch_enabled_prelude_handshake_and_deadline_match(self):
        import contextlib, io, json, shutil, threading
        from agent_hub_host import cli
        from agent_hub_host.quota_dispatch_manager import FanotifyReader
        root = self._short_temp()
        cfg = root / 'v2.json'
        broker_dir = root / 'broker'
        gate_dir = root / 'gate'
        src_dir = root / 'src'
        for d in (broker_dir, gate_dir, src_dir):
            d.mkdir(mode=0o700)
        repo_root = Path(__file__).resolve().parents[3]
        shutil.copyfile(repo_root / 'scripts/hub_alert_client.py', src_dir / 'hub_alert_client.py')
        shutil.copyfile(repo_root / 'scripts/hub-alert-quota-dispatch.py', src_dir / 'hub-alert-quota-dispatch.py')
        (src_dir / 'hub_alert_client.py').chmod(0o400)
        (src_dir / 'hub-alert-quota-dispatch.py').chmod(0o400)
        observer_seen = []
        self._write_readonly(
            src_dir, 'hub-quota-publish.py',
            b'def main(argv=None, *, _managed_observer=None):\n'
            b'    import builtins\n'
            b'    builtins._test_observer_seen.append(_managed_observer is not None)\n'
            b'    if _managed_observer is not None:\n'
            b'        assert _managed_observer.emit("begin") is True\n'
            b'        assert _managed_observer.emit("decision", decision="observation", condition="quota_fresh") is True\n'
            b'        assert _managed_observer.emit("terminal") is True\n'
            b'    return 0\n',
        )
        pid = os.getpid()
        cgroup = next(
            line.split(':', 2)[-1]
            for line in Path(f'/proc/{pid}/cgroup').read_text().splitlines()
            if ':' in line
        )
        cfg.write_text(json.dumps({
            'quota_same_process': {
                'v': 1, 'enabled': True,
                'broker_directory': str(broker_dir),
                'gate_directory': str(gate_dir),
                'source_directory': str(src_dir),
                'unit': 'agent-hub-quota-monitor.service',
                'cgroup': cgroup,
            }
        }))
        r_fan, w_fan = os.pipe()
        os.set_blocking(r_fan, False)
        seen_launch = []
        alive_state = {'alive': True}
        q_path_str = str(src_dir / 'hub-quota-publish.py')

        def _fake_subprocess_run(argv, **kw):
            if argv[0] == '/usr/bin/systemd-run':
                seen_launch.append(argv)
                idx = argv.index('--')
                prelude_argv = argv[idx + 1:]
                spec = importlib.util.spec_from_file_location('actual_prelude', prelude_argv[3])
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                orig_acquired = mod.acquired
                def _acquired_with_fanotify(p, d):
                    raw = orig_acquired(p, d)
                    if p == q_path_str:
                        ev_fd = os.open(q_path_str, os.O_RDONLY)
                        pkt = FanotifyReader._STRUCT.pack(24, 3, 0, 24, FanotifyReader._FAN_OPEN, ev_fd, pid)
                        os.write(w_fan, pkt)
                    return raw
                mod.acquired = _acquired_with_fanotify
                def _run():
                    try:
                        mod.main(prelude_argv[4:])
                    finally:
                        alive_state['alive'] = False
                t = threading.Thread(target=_run, daemon=True)
                t.start()
                t.join(timeout=2.0)
                return SimpleNamespace(returncode=0, stdout=b'')
            if '--property=ExecStart' in argv:
                prelude_cmd = seen_launch[-1][seen_launch[-1].index('--') + 1:]
                return SimpleNamespace(returncode=0, stdout=('ExecStart=' + ' '.join(prelude_cmd) + '\n').encode())
            if '--property=ExecMainCode' in argv:
                return SimpleNamespace(returncode=0, stdout=b'ExecMainCode=1\nExecMainStatus=0\n')
            main_pid = str(pid) if alive_state['alive'] else '0'
            return SimpleNamespace(
                returncode=0,
                stdout=f'MainPID={main_pid}\nControlGroup={cgroup}\nInvocationID={"a"*32}\n'.encode(),
            )

        out = io.StringIO()
        events_cb = {}
        def _fake_collector(_self, cb):
            events_cb['cb'] = cb
            return SimpleNamespace(
                wire=SimpleNamespace(receive=lambda *a: False), epoch='e'*32,
                fault=None, started=True, pending=None, jobs={},
                start=lambda: (cb({'member': 'JobNew', 'job_id': 1, 'unit': 'agent-hub-quota-monitor.service', 'result': None, 'stream_index': 3, 'manager_serial': 10, 'epoch': 'e'*32}), {'stream_index': 1})[1],
                handle=lambda *a: None,
                barrier=lambda: {'stream_index': 2}, close=lambda: None,
            )

        import builtins, select
        builtins._test_observer_seen = observer_seen
        self.addCleanup(lambda: delattr(builtins, '_test_observer_seen'))
        real_select = select.select
        def _fast_select(r, w, x, timeout=None):
            if timeout == 0.01:
                timeout = 0.001
            return real_select(r, w, x, timeout)

        def _fake_poll(self_b):
            if not alive_state['alive']:
                if not any(e.get('member') == 'JobRemoved' for e in events_cb['list']):
                    events_cb['cb']({'member': 'JobRemoved', 'job_id': 1, 'unit': 'agent-hub-quota-monitor.service', 'result': 'done', 'stream_index': 5, 'manager_serial': 11, 'epoch': 'e'*32})
                return {'stream_index': 6}
            return {'stream_index': 2}

        def _fake_collector2(_self, cb):
            events_cb['cb'] = lambda ev: (events_cb['list'].append(ev), cb(ev))[1]
            events_cb['list'] = []
            return SimpleNamespace(
                wire=SimpleNamespace(receive=lambda *a: False), epoch='e'*32,
                fault=None, started=True, pending=None, jobs={},
                start=lambda: (events_cb['cb']({'member': 'JobNew', 'job_id': 1, 'unit': 'agent-hub-quota-monitor.service', 'result': None, 'stream_index': 3, 'manager_serial': 10, 'epoch': 'e'*32}), {'stream_index': 1})[1],
                handle=lambda *a: None,
                barrier=lambda: {'stream_index': 2}, close=lambda: None,
            )

        with patch.object(wire.Broker, '_collector', _fake_collector2), \
             patch('select.select', _fast_select), \
             patch('agent_hub_host.quota_dispatch_manager.OrderedBarrier.begin', lambda self, d: None), \
             patch('agent_hub_host.quota_dispatch_manager.OrderedBarrier.poll', _fake_poll), \
             patch('os.pidfd_open', return_value=os.dup(r_fan)), \
             patch('agent_hub_host.quota_dispatch_manager.StableRuntime.alive', lambda self, fd: alive_state['alive']), \
             patch.object(FanotifyReader, 'open_group', return_value=r_fan), \
             patch('subprocess.run', side_effect=_fake_subprocess_run), \
             contextlib.redirect_stdout(out):
            rc = cli.main(['--config', str(cfg), 'quota-dispatch', '--enable'])
        self.assertEqual(rc, 0)
        self.assertEqual(len(seen_launch), 1)
        prelude_cmd = seen_launch[0][seen_launch[0].index('--') + 1:]
        self.assertEqual(prelude_cmd[3], str(src_dir / 'hub-alert-quota-dispatch.py'))
        self.assertTrue(prelude_cmd[4].isdigit() and int(prelude_cmd[4]) > 0)
        self.assertEqual(observer_seen, [True])
        payload = json.loads(out.getvalue())
        self.assertTrue(payload['dispatched'])
        self.assertTrue(payload['reserved'])
        self.assertEqual(payload['phase'], 'done')
        self.assertFalse(payload['complete'])
        try:
            os.close(w_fan)
        except OSError:
            pass

    def test_cli_quota_dispatch_enabled_prep_failure_runs_verified_retained_original(self):
        import contextlib, io, json
        from agent_hub_host import cli
        from agent_hub_host.quota_dispatch_manager import FanotifyReader
        root = self._short_temp()
        cfg = root / 'v2.json'
        broker_dir = root / 'broker'
        gate_dir = root / 'gate'
        src_dir = root / 'src'
        for d in (broker_dir, gate_dir, src_dir):
            d.mkdir(mode=0o700)
        self._write_readonly(
            src_dir, 'hub-quota-publish.py',
            b'def main(argv=None, *, _managed_observer=None):\n'
            b'    return 0 if _managed_observer is None else 9\n',
        )
        self._write_readonly(src_dir, 'hub_alert_client.py', b'print("c")\n')
        self._write_readonly(src_dir, 'hub-alert-quota-dispatch.py', b'print("p")\n')
        cfg.write_text(json.dumps({
            'quota_same_process': {
                'v': 1, 'enabled': True,
                'broker_directory': str(broker_dir),
                'gate_directory': str(gate_dir),
                'source_directory': str(src_dir),
                'unit': 'agent-hub-quota-monitor.service',
                'cgroup': '/user.slice/user-1000.slice/user@1000.service/app.slice/agent-hub-quota-monitor.service',
            }
        }))
        out = io.StringIO()
        with patch.object(FanotifyReader, 'open_group', side_effect=ram.Unknown('fanotify_unavailable')), \
             patch('subprocess.run', side_effect=AssertionError('must not invoke subprocess')), \
             contextlib.redirect_stdout(out):
            rc = cli.main(['--config', str(cfg), 'quota-dispatch', '--enable'])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload['fail_open_business'])
        self.assertFalse(payload['dispatched'])
        self.assertFalse(payload['complete'])

    def test_cli_quota_dispatch_source_pin_rejection_never_executes_rejected_path(self):
        import contextlib, io, json
        from agent_hub_host import cli
        root = self._short_temp()
        cfg = root / 'v2.json'
        broker_dir = root / 'broker'
        gate_dir = root / 'gate'
        src_dir = root / 'src'
        for d in (broker_dir, gate_dir, src_dir):
            d.mkdir(mode=0o700)
        bad = src_dir / 'hub-quota-publish.py'
        bad.write_bytes(b'raise AssertionError("rejected_path_executed")\n')
        bad.chmod(0o644)  # writable -> rejected by hold_source_fd
        cfg.write_text(json.dumps({
            'quota_same_process': {
                'v': 1, 'enabled': True,
                'broker_directory': str(broker_dir),
                'gate_directory': str(gate_dir),
                'source_directory': str(src_dir),
                'unit': 'agent-hub-quota-monitor.service',
                'cgroup': '/user.slice/user-1000.slice/user@1000.service/app.slice/agent-hub-quota-monitor.service',
            }
        }))
        out = io.StringIO()
        with patch('subprocess.run', side_effect=AssertionError('must not invoke subprocess')), \
             contextlib.redirect_stdout(out):
            rc = cli.main(['--config', str(cfg), 'quota-dispatch', '--enable'])
        self.assertEqual(rc, 1)
        payload = json.loads(out.getvalue())
        self.assertTrue(payload['source_rejected'])
        self.assertFalse(payload['dispatched'])

    def test_prelude_retained_fd_fallback_on_swapped_path_never_evaluates_replacement(self):
        import hashlib
        root = self._short_temp()
        src_dir = root / 'src'
        src_dir.mkdir(mode=0o700)
        orig_bytes = b'def main(argv=None, *, _managed_observer=None):\n    return 17 if _managed_observer is None else 99\n'
        q_path = self._write_readonly(src_dir, 'hub-quota-publish.py', orig_bytes)
        c_path = self._write_readonly(src_dir, 'hub_alert_client.py', b'print("c")\n')
        q_sha = hashlib.sha256(orig_bytes).hexdigest()
        c_sha = hashlib.sha256(b'print("c")\n').hexdigest()
        retained_fd = os.open(str(q_path), os.O_RDONLY | os.O_NOFOLLOW)
        self.addCleanup(lambda: os.close(retained_fd))
        # Swap path on disk with hostile replacement
        q_path.unlink()
        self._write_readonly(src_dir, 'hub-quota-publish.py', b'raise AssertionError("replacement_executed")\n')
        repo_root = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location('actual_prelude_swap', repo_root / 'scripts/hub-alert-quota-dispatch.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        rc = mod.main(['0', q_sha, c_sha, str(root), str(q_path), str(c_path), '--', '--check'], retained_fd=retained_fd)
        self.assertEqual(rc, 17)

    def test_prelude_helper_client_failure_still_runs_primary_once_and_closes_channel(self):
        import hashlib, socket, threading
        root = self._short_temp()
        src_dir = root / 'src'
        src_dir.mkdir(mode=0o700)
        orig_bytes = b'def main(argv=None, *, _managed_observer=None):\n    return 23 if _managed_observer is None else 99\n'
        q_path = self._write_readonly(src_dir, 'hub-quota-publish.py', orig_bytes)
        c_path = self._write_readonly(src_dir, 'hub_alert_client.py', b'raise RuntimeError("broken_client")\n')
        q_sha = hashlib.sha256(orig_bytes).hexdigest()
        c_sha = hashlib.sha256(b'raise RuntimeError("broken_client")\n').hexdigest()
        gate_sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        gate_sock.bind(str(root / 'source-gate.sock'))
        gate_sock.listen(1)
        self.addCleanup(gate_sock.close)

        def _server():
            conn, _ = gate_sock.accept()
            with conn:
                self.assertEqual(conn.recv(256), b'held-before-quota-source')
                conn.send(b'open:' + b'a' * 32)
                self.assertEqual(conn.recv(256), b'quota-source-opened')
                conn.send(b'qualified-run')
                # Prelude must close channel when helper fails
                self.assertEqual(conn.recv(256), b'')

        t = threading.Thread(target=_server, daemon=True)
        t.start()
        repo_root = Path(__file__).resolve().parents[3]
        spec = importlib.util.spec_from_file_location('actual_prelude_helper', repo_root / 'scripts/hub-alert-quota-dispatch.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        dl_ns = str(int((time.monotonic() + 5.0) * 1e9))
        rc = mod.main([dl_ns, q_sha, c_sha, str(root), str(q_path), str(c_path), '--', '--check'])
        t.join(timeout=2.0)
        self.assertEqual(rc, 23)
