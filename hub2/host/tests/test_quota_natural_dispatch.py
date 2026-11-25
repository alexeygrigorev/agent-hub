"""Pure protocol/source tests. No runtime FD, bus, units, process or authority."""
import builtins
from dataclasses import replace
import io
from contextlib import redirect_stdout
from collections import deque
from contextlib import nullcontext
import hashlib
import runpy
import json
from pathlib import Path
import stat
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from agent_hub_host import alert_authority as ram
from agent_hub_host import alert_broker as broker
from agent_hub_host.quota_dispatch import QuotaDispatch
from agent_hub_host import quota_dispatch_manager as m
from agent_hub_host import quota_natural_resources as natural

ROOT=Path(__file__).resolve().parents[3]
# Ordinary import, no synthetic agent_hub_host/sys.modules facade.
prelude=SimpleNamespace(**runpy.run_path(str(ROOT/'scripts/natural-quota-prelude.py')))
quota=SimpleNamespace(**runpy.run_path(str(ROOT/'scripts/hub-quota-publish.py')))
CLIENT=(ROOT/'scripts/hub_alert_client.py').read_bytes()
BOOT='11111111-2222-4333-8444-555555555555'

class Probe:
    def __init__(self,cleanup):
        self.cleanup=cleanup;self.result=None;self.cleanup_failed=False;self.done=SimpleNamespace(is_set=lambda:False)
    def submit(self,name,function):
        try:self.result=m.ProbeResult(name,value=function())
        except BaseException as error:self.result=m.ProbeResult(name,error=error)
    def poll(self):result=self.result;self.result=None;return result
    def close(self):pass # No fake cleanup-complete merely from requested close.

class Barriers:
    def __init__(self,collector,now):self.pending=False
    def begin(self,end):
        if self.pending:raise ram.Unknown('two_barriers')
        self.pending=True
    def poll(self):self.pending=False;return {'stream_index':100}
    def close(self):pass

class Channel:
    def __init__(self,role,decisions=1):
        self.role=role;self.decisions=decisions;self.context=None;self.exited=False;self.opened=False
        self.packets=deque([json.dumps({'v':1,'op':'held','started_ns':1_000_000_000}).encode()]);self.sent=[]
    def setblocking(self,value):pass
    def recvmsg(self,*a):
        if self.packets:return self.packets.popleft(),[],0,None
        if self.exited:return b'',[],0,None
        raise BlockingIOError()
    def send(self,data):
        self.sent.append(data)
        if data.startswith(b'open:'):
            self.context=data[5:].decode();self.opened=True;self.packets.append(b'quota-source-opened')
        elif data==b'qualified-run':
            phases=['begin']+['decision']*self.decisions+['terminal']
            for seq,phase in enumerate(phases):
                frame=dict(v=1,invocation_id=self.context,parent_id=None,role=self.role,phase=phase,seq=seq,
                    decision='alert' if phase=='decision' and seq==1 else 'observation' if phase=='decision' else None,
                    condition='quota_stale' if phase=='decision' else None,flags={},recipient_class='dev' if phase=='decision' and seq==1 else 'none',observer_ok=True)
                self.packets.append(json.dumps({'v':1,'op':'hook','frame':frame}).encode())
        elif data.startswith(b'{'):
            reply=json.loads(data)
            if reply['seq']==self.decisions+1:self.exited=True
        return len(data)

class Resources(m.NaturalResources):
    def __init__(self,selection,events,channels):self.s=selection;self.events=events;self.channels=deque(channels);self.closed=[];self.retired=[]
    def accept(self):return self.channels.popleft() if self.channels else None
    def drain(self):pass # Explicit fake watch; never runtime coverage.
    def bind(self,channel,started,deadline):
        job=1 if channel.role=='quota_publish' else 2
        return m.NaturalLease(m.NATURAL_UNITS[channel.role],job,f'{job:032x}',ram.ProcessKey(100+job,3,BOOT),90+job,
            channel,self.s.roles[channel.role].source,started)
    def current(self,lease):
        if lease.channel.exited:raise ram.Unknown('pidfd_exited')
    def source_event(self,lease):return lease.channel if lease.channel.opened else None
    def exact_open(self,lease,event):
        if event is not lease.channel:raise ram.Unknown('foreign_open')
    def exited(self,lease):return lease.channel.exited
    def retirement(self,lease):
        self.events.append(dict(member='JobRemoved',unit=lease.unit,job_id=lease.job_id,result='done',epoch='manager',stream_index=10+lease.job_id))
        return lease,0
    def close_owned(self,channels,leases):self.closed.append((channels,leases))
    def retire_owned(self,lease):
        if any(value is lease for value in self.retired):raise ram.Unknown('duplicate_retirement')
        self.retired.append(lease);return lease

class Tests(unittest.TestCase):
    def setUp(self):
        for target in ['socket.socket','subprocess.Popen','subprocess.run','os.pidfd_open','threading.Thread']:
            p=patch(target,side_effect=AssertionError('real runtime forbidden'));p.start();self.addCleanup(p.stop)
        self.raws={fd:('source'+str(fd)).encode() for fd in range(30,34)}
        def info(fd):
            directory=fd in (10,20);raw=self.raws.get(fd,b'')
            return SimpleNamespace(st_dev=1,st_ino=1 if fd==10 else fd,st_size=len(raw),st_uid=0,
                st_mode=(stat.S_IFDIR|0o755) if directory else (stat.S_IFREG|0o444),st_nlink=1,st_mtime_ns=1,st_ctime_ns=1)
        self.info=info
        def named(name,dir_fd=None,follow_symlinks=False):
            if name=='/':return info(10)
            if name=='owned':return info(20)
            if type(name) is str and name.startswith('s') and name[1:].isdigit():return info(int(name[1:]))
            raise FileNotFoundError('fake metadata only')
        for target,fn in [('os.fstat',info),('os.stat',named),('os.pread',lambda fd,n,offset:self.raws[fd]),('os.geteuid',lambda:0),('agent_hub_host.quota_dispatch_manager._natural_access_flags',lambda *a:0)]:
            p=patch(target,side_effect=fn);p.start();self.addCleanup(p.stop)
        sources=tuple(m.HeldSourceFD(fd,20,'s'+str(fd),raw,(),ram.SourcePin(1,fd,hashlib.sha256(raw).hexdigest())) for fd,raw in self.raws.items())
        plans=tuple(m.NaturalRole(role,*sources,('/usr/bin/python3','/root-owned/prelude.py',role),
            () if role=='quota_publish' else ('--check','--health-check','--health-config','/owned/v2.json','--health-timeout','8','--alert-timeout','8'),((10,'owned',20),)) for role in m.NATURAL_UNITS)
        self.plans=plans;self.selection=m.PeriodicQuotaSelection.from_root(plans)
    def manager(self,channels):
        events=[dict(member='JobNew',unit=m.NATURAL_UNITS[role],job_id=i,epoch='manager',stream_index=i+1) for i,role in [(1,'quota_publish'),(2,'quota_monitor')]]
        resources=Resources(self.selection,events,channels);collector=SimpleNamespace(epoch='manager',fault=None)
        manager=m.PeriodicQuotaManager(self.selection,collector,events,resources,now=lambda:1.01,probe_factory=Probe,barrier_factory=Barriers)
        manager.initial={'stream_index':0};manager.barrier_mode=None
        pins={role:self.plans[0].source.pin for role in ram.ROLES} # Pure full constructor, NOT source authority.
        live=ram.LiveRAM(pins,broker.checked,now=lambda:100)
        live.poison(ram.PRODUCERS,'dispatch_unqualified')
        owner=QuotaDispatch(SimpleNamespace(ram=live),manager,now=lambda:1.01)
        return manager,owner,live,resources
    def test_root_selection_is_not_a_producer_dictionary_or_user_path(self):
        with self.assertRaises(ram.Unknown):m.PeriodicQuotaSelection()
        with patch('os.geteuid',return_value=1000):
            with self.assertRaises(ram.Unknown):m.PeriodicQuotaSelection.from_root(self.plans)
        with patch.object(m,'_natural_access_flags',return_value=2):
            with self.assertRaises(ram.Unknown):m.PeriodicQuotaSelection.from_root(self.plans)
        with patch('os.pread',return_value=b'changed'):
            with self.assertRaises(ram.Unknown):m.PeriodicQuotaSelection.from_root(self.plans)
    def test_original_chronology_cannot_renew_or_accept_future_clock(self):
        self.assertEqual(m.original_hold(1_000_000_000,1.149),1.15)
        for start,now in [(1_000_000_000,1.15),(1_000_000_001,1.),(True,1.),(0,1.),(1,float('nan'))]:
            with self.assertRaises(ram.Unknown):m.original_hold(start,now)
    def test_two_roles_progress_without_starts_stops_or_global_healing(self):
        manager,owner,live,_=self.manager([Channel('quota_publish',2),Channel('quota_monitor',2)])
        for _ in range(250):manager.step(owner)
        self.assertEqual([a['phase'] for a in manager.attempts],['done','done'])
        self.assertEqual(live.counts['quota_stale'],{'started':2,'finished':2,'occurrences':2})
        self.assertEqual(set(live.faults.values()),{'dispatch_unqualified'})
        self.assertEqual(m.NATURAL_FRAMES,{'quota_publish':4,'quota_monitor':6})
    def test_settled_attempts_release_capacity_without_erasing_epoch_or_tombstones(self):
        manager,owner,live,resources=self.manager([])
        manager.events.clear()
        stream=[0]
        def event(member,job,role):
            stream[0]+=1
            value=dict(member=member,unit=m.NATURAL_UNITS[role],job_id=job,
                       epoch='manager',stream_index=stream[0])
            if member=='JobRemoved':value['result']='done'
            manager.events.append(value)
        def bind(channel,started,deadline):
            job=channel.job
            return m.NaturalLease(m.NATURAL_UNITS[channel.role],job,f'{job:032x}',
                ram.ProcessKey(100+job,3,BOOT),90+job,channel,
                self.selection.roles[channel.role].source,started)
        def retirement(lease):
            role=next(role for role,unit in m.NATURAL_UNITS.items() if unit==lease.unit)
            event('JobRemoved',lease.job_id,role)
            return lease,0
        def barrier():
            stream[0]+=1
            manager._barriers.pending=False
            return {'stream_index':stream[0]}
        resources.bind=bind;resources.retirement=retirement
        manager._barriers.poll=barrier
        epoch=live.epoch
        for job in range(1,131):
            role='quota_publish' if job%2 else 'quota_monitor'
            channel=Channel(role,2);channel.job=job
            event('JobNew',job,role);resources.channels.append(channel)
            for _ in range(50):manager.step(owner)
            self.assertFalse(manager.loss,job)
            self.assertEqual(live.counts['quota_stale']['finished'],job)
            self.assertEqual(manager.channels,[])
            self.assertEqual(manager.leases,[])
        self.assertEqual(live.epoch,epoch)
        self.assertEqual(len(manager.jobs),130)
        self.assertEqual(len(owner.seen),130)
        self.assertEqual(len(resources.retired),130)
        self.assertEqual(set(live.faults.values()),{'dispatch_unqualified'})
        event('JobNew',1,'quota_publish')
        manager.step(owner)
        self.assertTrue(manager.loss,'settled Job identity replay must remain rejected')
    def test_active_capacity_is_not_reused_before_cleanup_probe_ack(self):
        manager,owner,_,resources=self.manager([Channel('quota_monitor',2)])
        for _ in range(100):
            manager.step(owner)
            if manager.attempts and manager.attempts[0]['phase']=='cleanup_poll':break
        self.assertEqual(manager.attempts[0]['phase'],'cleanup_poll')
        self.assertEqual(len(resources.retired),1)
        with patch.object(manager.probe,'poll',return_value=None):
            for _ in range(10):manager.step(owner)
            self.assertEqual(len(manager.channels),1)
            self.assertEqual(len(manager.leases),1)
            with self.assertRaises(ram.Unknown):manager.barrier(1.1)
        manager.step(owner)
        self.assertEqual(manager.attempts[0]['phase'],'done')
        self.assertEqual(manager.channels,[])
        self.assertEqual(manager.leases,[])
    def test_notification_loss_after_open_prevents_terminal_settlement(self):
        manager,owner,live,resources=self.manager([Channel('quota_monitor',2)])
        for _ in range(100):
            manager.step(owner)
            if manager.attempts and manager.attempts[0]['phase']=='hooks':break
        self.assertTrue(manager.attempts[0]['ticket'].opened)
        with patch.object(resources,'drain',side_effect=ram.Unknown('late_overflow')):
            manager.step(owner)
        self.assertTrue(manager.loss)
        self.assertNotEqual(manager.attempts[0]['phase'],'done')
        self.assertEqual(live.counts['quota_stale']['finished'],0)
        with self.assertRaises(ram.Unknown):manager.barrier(1.1)
    def test_notification_loss_during_final_poll_cannot_finish_attempt(self):
        manager,owner,live,resources=self.manager([Channel('quota_monitor',2)])
        for _ in range(150):
            manager.step(owner)
            if manager.attempts and manager.attempts[0]['phase']=='barrier_poll':break
        self.assertEqual(manager.attempts[0]['phase'],'barrier_poll')
        original=manager._barriers.poll
        fault=[]
        def poll():
            result=original();fault.append(True);return result
        def drain():
            if fault:raise ram.Unknown('overflow_during_barrier')
        with patch.object(manager._barriers,'poll',side_effect=poll),patch.object(resources,'drain',side_effect=drain):
            manager.step(owner)
        self.assertTrue(manager.loss)
        self.assertEqual(live.counts['quota_stale']['finished'],0)
        self.assertNotEqual(manager.attempts[0]['phase'],'done')
    def test_snapshot_barrier_drains_after_already_settled_attempts(self):
        manager,owner,live,resources=self.manager([Channel('quota_monitor',2)])
        for _ in range(150):manager.step(owner)
        self.assertEqual(manager.attempts[0]['phase'],'done')
        self.assertEqual(live.counts['quota_stale']['finished'],1)
        with patch.object(resources,'drain',side_effect=ram.Unknown('late_foreign_open')):
            with self.assertRaises(ram.Unknown):manager.barrier(1.1)
        self.assertTrue(manager.loss)
        with self.assertRaises(ram.Unknown):manager.barrier(1.1)
    def test_future_hold_never_admits(self):
        channel=Channel('quota_publish');channel.packets=deque([b'{"v":1,"op":"held","started_ns":2000000000}'])
        manager,owner,live,_=self.manager([channel])
        for _ in range(20):manager.step(owner)
        self.assertEqual(live.counts['quota_stale']['started'],0);self.assertTrue(manager.loss)
    def test_close_is_not_borrowed_fd_retirement(self):
        manager,owner,_,resources=self.manager([]);manager.close()
        self.assertFalse(manager.resources_retired);self.assertEqual(resources.closed,[])
    def selected(self,role='quota_publish'):
        raw=b"def main(argv, _managed_observer=None):\n    return __import__('builtins')._natural_test_effect(argv,_managed_observer)\n"
        return prelude.SelectedQuota(role,raw,hashlib.sha256(raw).hexdigest(),CLIENT,hashlib.sha256(CLIENT).hexdigest(),())
    def test_prelude_broker_absence_and_source_error_run_original_once(self):
        for fault in ['connect','source','late','clock']:
            calls=[];selected=self.selected();channel=SimpleNamespace(send=lambda *a:None,recv=lambda *a:b'open:'+b'a'*32,close=lambda:None)
            def connect(end):
                if fault=='connect':raise OSError('unavailable')
                return channel
            def opened(*a):
                if fault=='source':raise OSError('lost source')
            with patch.object(builtins,'_natural_test_effect',lambda argv,observer:calls.append((argv,observer)) or 7,create=True):
                rc=prelude.run_selected(selected,opened,connect,now=lambda:1.2 if fault=='late' else 1.01,now_ns=lambda:None if fault=='clock' else 1_000_000_000)
            self.assertEqual(rc,7);self.assertEqual(calls,[([],None)])
    def test_original_control_exception_is_preserved_without_business_retry(self):
        calls=[];error=KeyboardInterrupt('owned-control')
        def effect(*a):calls.append(a);raise error
        with patch.object(builtins,'_natural_test_effect',effect,create=True):
            with self.assertRaises(KeyboardInterrupt) as caught:
                prelude.run_selected(self.selected(),lambda *a:None,lambda end:(_ for _ in ()).throw(OSError()),now=lambda:1.01,now_ns=lambda:1_000_000_000)
        self.assertIs(caught.exception,error);self.assertEqual(len(calls),1)
    def test_publisher_four_frame_bound_comes_from_real_publish_recovery(self):
        events=[];observer=SimpleNamespace(emit=lambda phase,**kw:events.append((phase,kw)))
        args=SimpleNamespace(hub='/fake/hub',publish_timeout=120,state='/fake/state',stale_seconds=840)
        state={'accounts':{'codex':0},'alerted':['codex']}
        with patch.object(quota.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='fake')),\
             patch.dict(quota.publish.__globals__,{'receipt':lambda raw:['codex'],'now_ms':lambda:100,
                'locked':lambda *a:nullcontext(),'read_state':lambda *a:state,'write_state':lambda *a:None}):
            quota.publish(args,observer)
        self.assertEqual(events,[('decision',{'decision':'observation','condition':'quota_publication','recipient_class':'none'}),
            ('decision',{'decision':'recovery','condition':'quota_stale','recipient_class':'none'})])
        self.assertEqual(len(events)+2,m.NATURAL_FRAMES['quota_publish'])

    def test_missing_original_job_and_foreign_unit_never_admit(self):
        for mode in ['missing-job','foreign-unit']:
            channel=Channel('quota_publish');manager,owner,live,resources=self.manager([channel])
            if mode=='missing-job':manager.events.clear()
            else:
                bind=resources.bind
                def foreign(*a):
                    lease=bind(*a);lease.unit='foreign.service';return lease
                resources.bind=foreign
            for _ in range(40):manager.step(owner)
            self.assertEqual(live.counts['quota_stale']['started'],0,mode)
            self.assertTrue(manager.loss,mode)
    def test_notification_loss_poison_and_later_timer_cannot_heal(self):
        manager,owner,live,resources=self.manager([Channel('quota_publish')])
        resources.exact_open=lambda *a:(_ for _ in ()).throw(ram.Unknown('watch_overflow'))
        for _ in range(40):manager.step(owner)
        self.assertTrue(manager.loss)
        resources.channels.append(Channel('quota_monitor'))
        for _ in range(40):manager.step(owner)
        self.assertEqual(live.counts['quota_stale']['finished'],0)
        self.assertNotEqual(live.faults['quota_stale'],None)
    def test_public_context_replay_does_not_replace_bound_channel(self):
        manager,owner,live,_=self.manager([Channel('quota_publish')])
        for _ in range(80):manager.step(owner)
        context=manager.attempts[0]['ticket'].context
        self.assertEqual(owner.public_hook(context),{'seq':-1,'ok':False})
        self.assertNotEqual(live.faults['quota_stale'],None)
    def test_ancillary_held_data_refuses_before_probe(self):
        channel=Channel('quota_publish')
        channel.recvmsg=lambda *a:(b'{}',[(1,2,b'x')],0,None)
        manager,owner,live,_=self.manager([channel])
        for _ in range(20):manager.step(owner)
        self.assertTrue(manager.loss);self.assertEqual(live.counts['quota_stale']['started'],0)
    def test_expired_original_hold_after_source_open_does_not_renew(self):
        calls=[];clock=[1.01]
        class C:
            def __init__(self):self.reads=0
            def send(self,*a):pass
            def recv(self,*a):
                self.reads+=1
                if self.reads==1:return b'open:'+b'a'*32
                clock[0]=1.151;return b'qualified-run'
            def close(self):pass
        with patch.object(builtins,'_natural_test_effect',lambda argv,observer:calls.append(observer),create=True):
            prelude.run_selected(self.selected(),lambda *a:None,lambda end:C(),now=lambda:clock[0],now_ns=lambda:1_000_000_000)
        self.assertEqual(calls,[None])
    def test_prelude_real_client_frames_are_only_transport_not_admission(self):
        packets=[]
        class C:
            def __init__(self):self.reads=0;self.last=None
            def send(self,raw,end):self.last=raw;packets.append(raw)
            def recv(self,end):
                self.reads+=1
                if self.reads==1:return b'open:'+b'a'*32
                if self.reads==2:return b'qualified-run'
                return json.dumps({'ok':True,'seq':json.loads(self.last)['frame']['seq']}).encode()
            def close(self):pass
        def effect(argv,observer):
            self.assertIsNotNone(observer)
            observer.emit('begin')
            observer.emit('decision',decision='observation',condition='quota_publication')
            observer.emit('terminal');return 9
        with patch.object(builtins,'_natural_test_effect',effect,create=True):
            self.assertEqual(prelude.run_selected(self.selected(),lambda *a:None,lambda end:C(),now=lambda:1.01,now_ns=lambda:1_000_000_000),9)
        frames=[json.loads(p)['frame'] for p in packets if p.startswith(b'{') and json.loads(p).get('op')=='hook']
        self.assertEqual([f['phase'] for f in frames],['begin','decision','terminal'])
        self.assertEqual([f['seq'] for f in frames],[0,1,2])

    def test_clock_reversal_cannot_extend_a_natural_attempt(self):
        manager,owner,live,_=self.manager([])
        self.assertEqual(manager.now(),1.01)
        manager._time=lambda:1.005
        with self.assertRaises(ram.Unknown):manager.now()

    def test_valid_original_business_runs_once_with_bad_or_missing_client(self):
        # Genuine committed quota bytes: execute its actual main AND publish.
        raw=(ROOT/'scripts/hub-quota-publish.py').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'319c93fd0b4f7239a3bb0d69f083a0e938122f02446e2e7c36035cbc61b9bc21')
        for client,digest in [(b'changed','0'*64),(None,'0'*64),(b'',hashlib.sha256(b'').hexdigest()),
                ('not-bytes','0'*64),(b'x'*262145,'0'*64)]:
            selected=prelude.SelectedQuota('quota_publish',raw,hashlib.sha256(raw).hexdigest(),client,digest,())
            effects=[];channels=[];compiled=[]
            def executing(code,namespace):
                builtins.exec(code,namespace)
                compiled.append(namespace)
                # Stub only external time/state effects after loading verified
                # source. Actual main/publish/receipt/observe are unchanged.
                namespace.update(now_ms=lambda:100,locked=lambda *a:nullcontext(),
                    read_state=lambda *a:{'accounts':{'codex':0},'alerted':[]},write_state=lambda *a:None)
            def effect(*a,**kw):
                effects.append(a);return SimpleNamespace(returncode=0,stdout='{"published":["codex"],"skipped":{}}')
            with patch.dict(prelude.run_selected.__globals__,{'exec':executing}),                 patch.object(quota.subprocess,'run',side_effect=effect),redirect_stdout(io.StringIO()):
                result=prelude.run_selected(selected,lambda *a:None,lambda *a:channels.append(a),now=lambda:1.01,now_ns=lambda:1_000_000_000)
            self.assertEqual(result,0);self.assertEqual(len(effects),1)
            self.assertEqual(channels,[],'bad optional client does not even connect')
            self.assertEqual(len(compiled),1)
    def test_bad_original_business_bytes_still_fail_closed(self):
        effects=[]
        with patch.object(builtins,'_natural_test_effect',lambda *a:effects.append(a),create=True):
            with self.assertRaises(ValueError):
                prelude.run_selected(replace(self.selected(),quota=b'changed'),lambda *a:None,lambda *a:None,
                    now=lambda:1.01,now_ns=lambda:1_000_000_000)
        self.assertEqual(effects,[])
    def test_client_import_failure_is_optional_but_control_identity_is_original(self):
        for raw in [b'not valid python !',b'raise ValueError("observer unavailable")']:
            selected=replace(self.selected(),client=raw,client_sha256=hashlib.sha256(raw).hexdigest())
            calls=[];closed=[]
            class C:
                def __init__(self):self.reads=0
                def send(self,*a):pass
                def recv(self,*a):
                    self.reads+=1;return b'open:'+b'a'*32 if self.reads==1 else b'qualified-run'
                def close(self):closed.append(True)
            control=KeyboardInterrupt('original-business-control')
            def effect(*a):calls.append(a);raise control
            with patch.object(builtins,'_natural_test_effect',effect,create=True):
                with self.assertRaises(KeyboardInterrupt) as caught:
                    prelude.run_selected(selected,lambda *a:None,lambda *a:C(),now=lambda:1.01,now_ns=lambda:1_000_000_000)
            self.assertIs(caught.exception,control);self.assertEqual(len(calls),1);self.assertEqual(closed,[True])

    def test_interpreter_is_full_executable_not_a_script_sized_stub(self):
        raw=b'ELF'+b'x'*(1024*1024)
        self.raws[33]=raw
        pin=ram.SourcePin(1,33,hashlib.sha256(raw).hexdigest())
        with self.assertRaises(ram.Unknown):
            m.HeldSourceFD(33,20,'s33',raw,(),pin)
        interpreter=m.HeldExecutableFD(33,20,'s33',raw,(),pin)
        plans=tuple(replace(p,interpreter=interpreter) for p in self.plans)
        selected=m.PeriodicQuotaSelection.from_root(plans)
        selected.current(plans[0],full=True)
        with patch('os.pread',side_effect=lambda fd,n,offset:
                   raw[:-1]+b'y' if fd==33 else self.raws[fd]):
            with self.assertRaises(ram.Unknown):
                m.PeriodicQuotaSelection.from_root(plans)


class LinuxResourceTests(unittest.TestCase):
    """Actual adapter logic with every OS/manager boundary replaced, not a
    runtime witness. Failures cover incarnation, replay, exit and ownership.
    """
    def setUp(self):
        for target in ('socket.socket','subprocess.Popen','subprocess.run',
                       'os.pidfd_open','os.close','builtins.open'):
            p=patch(target,side_effect=AssertionError('real runtime forbidden'))
            p.start();self.addCleanup(p.stop)
        self.closed=[];self.alive=True;self.birth=3;self.queries=[]
        self.r=object.__new__(natural.LinuxNaturalResources)
        self.r.now=lambda:1.01
        self.r.closed=self.r.fault=False
        self.r.retirement_failed=False
        self.r.owned=[];self.r.channels=[]
        self.r.group=777;self.r.observation=m.FanotifyObservation()
        self.r.fan=SimpleNamespace(drain=lambda *a:None)
        source=m.HeldSourceFD(30,20,'quota',b'source',(),
                              ram.SourcePin(1,30,hashlib.sha256(b'source').hexdigest()))
        self.plan=SimpleNamespace(source=source,exec_start=('/usr/bin/python3','/root/prelude'),
                                   interpreter=SimpleNamespace(pin=ram.SourcePin(1,33,'a'*64)))
        self.r.selection=SimpleNamespace(roles={role:self.plan for role in m.NATURAL_UNITS},
                                         current=lambda *a,**k:None)
        self.r.stable=SimpleNamespace(UID=1000,alive=lambda fd:self.alive,
            inspect_process=lambda pid,cg:(pid,self.birth,BOOT),
            inspect_exec=m.StableRuntime.inspect_exec)
        self.r.environment=m.UserManagerEnvironment(now=self.r.now)
        self.states={role:self.state(role,i+101) for i,role in enumerate(m.NATURAL_UNITS)}
        def query(argv,timeout):
            self.queries.append(argv)
            self.assertEqual(argv[:4],['/usr/bin/systemctl','--user','show','--all'])
            role=next(r for r,u in m.NATURAL_UNITS.items() if u==argv[4])
            return 0, ''.join(k+'='+v+'\n' for k,v in self.states[role].items()).encode()
        self.r.runner=SimpleNamespace(run=query)
        p=patch.object(natural.LinuxNaturalResources,'_command',return_value=None)
        p.start();self.addCleanup(p.stop)
        p=patch('os.stat',side_effect=lambda path:SimpleNamespace(
            st_dev=1,st_ino=99 if str(path).endswith('/ns/time') else 33))
        p.start();self.addCleanup(p.stop)

    def state(self,role,pid):
        return dict(MainPID=str(pid),ControlGroup='/user/quota/'+role,
            InvocationID=('1' if role=='quota_publish' else '2')*32,
            ActiveState='activating',SubState='start',Job=str(pid),
            ExecStart='/usr/bin/python3 /root/prelude',ExecMainCode='0',
            ExecMainStatus='0',ExecMainStartTimestampMonotonic='900000')

    def channel(self,pid):
        channel=SimpleNamespace(getsockopt=lambda *a:struct.pack('3i',pid,1000,1000),
                                 close=lambda:self.closed.append(('channel',pid)))
        self.r.channels.append(channel)
        return channel

    def bind(self,pid):
        with patch('os.pidfd_open',side_effect=lambda p,flags:p+100):
            return self.r.bind(self.channel(pid),1_000_000_000,1.15)

    def test_job_is_current_positive_unit_job_not_an_unbound_start(self):
        for value in ('','0','-1','101 /org/job/101','4294967296'):
            self.states['quota_publish']['Job']=value
            with self.assertRaises(ram.Unknown):self.bind(101)
        self.assertEqual(self.r.owned,[])

    def test_pid_incarnation_and_interpreter_are_rechecked(self):
        lease=self.bind(101)
        self.birth=4
        with self.assertRaises(ram.Unknown):self.r.current(lease)
        self.birth=3
        with patch('os.stat',return_value=SimpleNamespace(st_dev=9,st_ino=9)):
            with self.assertRaises(ram.Unknown):self.r.current(lease)

    def test_both_role_events_are_routed_and_consumed_exactly_once(self):
        first,second=self.bind(101),self.bind(102)
        events=[dict(pid=p,dev=1,ino=30,mask=0x20) for p in (101,102)]
        self.r.observation.records.extend(events)
        self.assertIs(self.r.source_event(first),events[0])
        self.r.exact_open(first,events[0])
        self.assertIs(self.r.source_event(second),events[1])
        self.r.exact_open(second,events[1])
        with self.assertRaises(ram.Unknown):self.r.exact_open(second,events[1])
        self.assertEqual(self.r.observation.records,[])

    def test_repeated_source_open_is_sticky_unknown(self):
        lease=self.bind(101);event=dict(pid=101,dev=1,ino=30,mask=0x20)
        self.r.observation.records.append(event)
        self.r.exact_open(lease,self.r.source_event(lease))
        self.r.observation.records.append(dict(event))
        with self.assertRaises(ram.Unknown):self.r.source_event(lease)
        self.assertTrue(self.r.fault)
        self.r.observation.records.clear()
        with self.assertRaises(ram.Unknown):self.r.source_event(lease)

    def test_unselected_process_open_cannot_become_a_lease(self):
        lease=self.bind(101)
        self.r.observation.records.append(dict(pid=999,dev=1,ino=30,mask=0x20))
        with self.assertRaises(ram.Unknown):self.r.source_event(lease)
        self.assertTrue(self.r.fault)

    def _late_fault(self,condition):
        lease=self.bind(101)
        first=dict(pid=101,dev=1,ino=30,mask=0x20)
        self.r.observation.records.append(first)
        self.r.exact_open(lease,self.r.source_event(lease))
        if condition=='overflow':
            def drain(*a):self.r.observation.unknown=True
        else:
            event=dict(first)
            if condition=='foreign':event['pid']=999
            def drain(*a):self.r.observation.records.append(event)
        with patch.object(self.r.fan,'drain',side_effect=drain):
            with self.assertRaises(ram.Unknown):self.r.drain()
        self.assertTrue(self.r.fault)
        self.r.observation.unknown=False
        self.r.observation.records.clear()
        with self.assertRaises(ram.Unknown):self.r.drain()

    def test_post_open_drain_observes_duplicate(self):
        self._late_fault('duplicate')

    def test_post_open_drain_observes_foreign(self):
        self._late_fault('foreign')

    def test_post_open_drain_observes_overflow(self):
        self._late_fault('overflow')

    def test_retirement_does_not_borrow_the_next_invocation_status(self):
        lease=self.bind(101);self.r.owned[0].opened=True;self.alive=False
        state=self.states['quota_publish']
        state.update(MainPID='0',ActiveState='inactive',SubState='dead',
                     ExecMainCode='1',ExecMainStatus='0',Job='')
        original=self.r._show;calls=[]
        def changed(role,deadline):
            value=dict(original(role,deadline));calls.append(role)
            if len(calls)==2:value['InvocationID']='f'*32
            return value
        with patch.object(self.r,'_show',side_effect=changed):
            with self.assertRaises(ram.Unknown):self.r.retirement(lease)
        self.assertIsNone(self.r.owned[0].status)
        self.assertEqual(self.r.retirement(lease),(lease,0))

    def test_cleanup_owns_channels_and_pidfds_but_not_donated_sources(self):
        first,second=self.bind(101),self.bind(102)
        with patch('os.close',side_effect=lambda fd:self.closed.append(('fd',fd))):
            self.r.close_owned(tuple(self.r.channels),(first,second))
        self.assertEqual([x for x in self.closed if x[0]=='fd'],[('fd',201),('fd',202)])
        self.assertEqual(len([x for x in self.closed if x[0]=='channel']),2)
        with self.assertRaises(ram.Unknown):self.r.close_owned((),())

    def test_settled_handle_retirement_does_not_close_active_or_donated_handles(self):
        first,second=self.bind(101),self.bind(102)
        owned=self.r.owned[0];owned.opened=True;owned.status=0;first.released=True
        self.alive=False
        with patch('os.close',side_effect=lambda fd:self.closed.append(('fd',fd))):
            self.assertIs(self.r.retire_owned(first),first)
            with self.assertRaises(ram.Unknown):self.r.retire_owned(first)
            self.assertEqual([o.lease for o in self.r.owned],[second])
            self.assertEqual(self.r.channels,[second.channel])
            self.r.close_owned((second.channel,),(second,))
        self.assertEqual(self.closed,[('channel',101),('fd',201),('channel',102),('fd',202)])

    def test_unsettled_or_live_process_cannot_release_its_handles(self):
        lease=self.bind(101)
        owned=self.r.owned[0];owned.opened=True;owned.status=0;lease.released=True
        with patch('os.close')as close:
            with self.assertRaises(ram.Unknown):self.r.retire_owned(lease)
            self.alive=False;owned.status=None
            with self.assertRaises(ram.Unknown):self.r.retire_owned(lease)
            close.assert_not_called()
        self.assertEqual(self.closed,[])
        self.assertEqual(len(self.r.owned),1)

    def test_retirement_close_error_is_latched_and_never_retried_or_healed(self):
        lease=self.bind(101)
        owned=self.r.owned[0];owned.opened=True;owned.status=0;lease.released=True
        self.alive=False
        failure=OSError('uncertain channel close')
        lease.channel.close=lambda:(_ for _ in ()).throw(failure)
        with patch('os.close')as close:
            with self.assertRaises(OSError)as caught:self.r.retire_owned(lease)
            self.assertIs(caught.exception,failure)
            self.assertTrue(self.r.retirement_failed)
            self.assertTrue(self.r.fault)
            with self.assertRaises(ram.Unknown):self.r.close_owned((),())
            close.assert_called_once_with(201)

    def test_duplicate_properties_refuse_manager_evidence(self):
        raw=b''.join((k+'='+v+'\n').encode() for k,v in self.states['quota_publish'].items())
        with patch.object(self.r.runner,'run',return_value=(0,raw+b'Job=999\n')):
            with self.assertRaises(ram.Unknown):self.r._show('quota_publish',1.15)


class ArgumentIdentityTests(unittest.TestCase):
    def test_printed_argv_collision_cannot_qualify_a_process(self):
        plan=SimpleNamespace(exec_start=('/usr/bin/python3','/root/prelude','one argument'))
        good=b'/usr/bin/python3\0/root/prelude\0one argument\0'
        bad=b'/usr/bin/python3\0/root/prelude\0one\0argument\0'
        with patch('builtins.open',return_value=io.BytesIO(good)):
            natural.LinuxNaturalResources._command(101,plan)
        with patch('builtins.open',return_value=io.BytesIO(bad)):
            with self.assertRaises(ram.Unknown):
                natural.LinuxNaturalResources._command(101,plan)
