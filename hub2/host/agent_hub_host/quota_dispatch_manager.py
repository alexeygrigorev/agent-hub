"""Trusted V4 adapter for ONE selected quota-monitor session, not a producer API.

Construction requires existing Root-prepared held selection and fanotify group.
There is NO config/JSON factory or privileged setup/elevation here. Source FD,
bootstrap/client/interpreter selection and original manager connection are held
by the dispatcher, never supplied by a hook or a historical receipt.
"""
import ctypes
import hashlib
import json
import math
from dataclasses import dataclass
import os
import select
import socket
import stat
import struct
import subprocess
import time
import queue
import threading
from . import alert_authority as ram
from .alert_broker import decode, FRAME


# Pinned quota_monitor main: begin + <=2 health decisions (normal then error)
# + <=2 quota decisions (normal then error) + terminal. No quota_publish or
# generic tool protocol is admitted here. Business never waits on this cap.
MAX_QUOTA_MONITOR_FRAMES = 6


@dataclass(frozen=True)
class HeldSourceFD:
    fd: int
    directory_fd: int
    name: str
    raw: bytes
    stamp: tuple
    pin: ram.SourcePin
    def __post_init__(self):
        ram.require(type(self.fd) is int and self.fd>2 and type(self.directory_fd) is int and self.directory_fd>2
            and type(self.name) is str and '/' not in self.name and self.name not in ('','.','..')
            and type(self.raw) is bytes and 0<len(self.raw)<=262144
            and type(self.stamp) is tuple and type(self.pin) is ram.SourcePin)
        self.pin.checked()


@dataclass(frozen=True)
class PreparedQuotaSelection:
    # ONLY the separately reviewed Root-preparer may construct this from its
    # live owned FDs. Its unit start/accept/cleanup closures are trusted manager
    # CODE, never producer callbacks or JSON facts. No runtime constructor is
    # provided until that concrete pre-drop envelope is independently reviewed.
    stable: object
    manager_environment: object
    fan: object
    group: int
    observation: object
    source: HeldSourceFD
    companions: tuple
    runner: object
    plan: dict
    exec_argv: object
    poll_held: object
    cleanup_owned: object
    service_deadline: float
    def __post_init__(self):
        ram.require(type(self.source) is HeldSourceFD and type(self.companions) is tuple and len(self.companions)==2
            and all(type(h) is HeldSourceFD for h in self.companions)
            and type(self.group) is int and self.group>2 and callable(self.poll_held)
            and callable(self.cleanup_owned) and callable(self.exec_argv)
            and type(self.service_deadline) in (int,float) and math.isfinite(self.service_deadline)
            and 0<self.service_deadline-time.monotonic()<=45)


class SourceSession:
    __slots__ = ('manager', 'process', 'pidfd', 'channel', 'invocation', 'cgroup', 'epoch', 'initial', 'released')
    def __init__(self, manager, process, pidfd, channel, invocation, cgroup, initial):
        self.manager, self.process, self.pidfd, self.channel = manager, process, pidfd, channel
        self.invocation, self.cgroup = invocation, cgroup
        self.epoch, self.initial, self.released = manager.epoch, initial, False


class ProbeResult:
    """Internal single-use completion token, never deserialized from clients."""
    __slots__ = ('operation', 'value', 'error')
    def __init__(self, operation, value=None, error=None):
        self.operation, self.value, self.error = operation, value, error


class SerialProbe:
    """One trusted metadata worker. No RAM or Collector access on this thread.

    Every task has an existing absolute deadline; no retries. close requests
    cleanup but NEVER waits on the owner loop. Caller must retain borrowed FDs
    until done is set; late completion is discarded, never qualified.
    """
    def __init__(self, cleanup):
        self.input, self.output = queue.Queue(1), queue.Queue(1)
        self.stopping, self.done = threading.Event(), threading.Event()
        self.cleanup = cleanup
        self.cleanup_failed = False
        self.thread = threading.Thread(target=self.run, name='quota-source-probe', daemon=True)
        self.thread.start()
    def submit(self, operation, function):
        ram.require(not self.stopping.is_set())
        self.input.put_nowait((operation, function))
    def poll(self):
        try: return self.output.get_nowait()
        except queue.Empty: return None
    def run(self):
        try:
            while not self.stopping.is_set():
                try: operation, function = self.input.get(timeout=.01)
                except queue.Empty: continue
                try: result = ProbeResult(operation, value=function())
                except BaseException as error: result = ProbeResult(operation, error=error)
                if self.stopping.is_set(): break
                try: self.output.put_nowait(result)
                except queue.Full: break
                if result.error is not None and not isinstance(result.error, Exception): break
        finally:
            try: self.cleanup()
            except BaseException:
                self.cleanup_failed = True # fixed state only; no raw thread traceback
            finally: self.done.set()
    def close(self): self.stopping.set()


class OrderedBarrier:
    """ListJobs on the SAME collector connection, split send/poll, no wait loop.

    Uses the original Collector strict reply parser, sender/serial/signature and
    job-set checks. A pending reply remains collector-owned; timeout poisons it.
    Other synchronous calls on that connection must not run while pending.
    """
    def __init__(self, collector, now=time.monotonic):
        self.c, self.now, self.pending, self.end = collector, now, None, None
    def begin(self, deadline):
        c=self.c
        ram.require(self.pending is None and c.pending is None and c.started and c.fault is None and self.now()<deadline)
        serial=c.wire.send('org.freedesktop.systemd1','/org/freedesktop/systemd1','org.freedesktop.systemd1.Manager','ListJobs',None)
        ram.require(type(serial) is int and 0<serial<=0xffffffff)
        self.pending={'serial':serial,'sender':c.manager,'signature':'a(usssoo)','reply':None}
        self.end=min(deadline,self.now()+.15);c.pending=self.pending
    def poll(self):
        c=self.c
        ram.require(self.pending is not None and c.pending is self.pending and c.fault is None)
        if self.now()>=self.end:
            c.fault=c.fault or 'deadline_or_transport'
            raise ram.Unknown('manager_barrier_deadline')
        reply=self.pending['reply']
        if reply is None:return None
        rows,index=reply
        ram.require(len({r[0] for r in rows})==len(rows) and {r[0]:r[1] for r in rows}==c.jobs)
        receipt=c.receipt(index);c.pending=None;self.pending=None
        return receipt
    def close(self):
        if self.pending is not None:
            self.c.fault=self.c.fault or 'barrier_unqualified'
            if self.c.pending is self.pending:self.c.pending=None
            self.pending=None


class V4QuotaManager:
    """Incremental owner-thread protocol plus one serial trusted OS probe.

    step performs ONE phase action, never sleeps/waits for IO/exit/worker or a
    D-Bus reply. Metadata subprocesses and full FD rereads execute only in the
    trusted probe worker; RAM, source-open correlation and original Collector
    connection remain on the owner. No runtime construction exists until the
    separately reviewed Root preparer supplies its actual held selection.
    """
    def __init__(self, prepared, collector, events, *, now=time.monotonic, probe_factory=SerialProbe):
        ram.require(type(prepared) is PreparedQuotaSelection)
        self.p=prepared;self.stable=prepared.stable;self.env=prepared.manager_environment
        self.fan=prepared.fan;self.group=prepared.group;self.observation=prepared.observation
        self.collector=collector;self.events=events;self.source=prepared.source;self.epoch=collector.epoch
        self.now=now;self.initial=None;self.phase='initial_begin';self.ticket=None
        self.lease=None;self.pending_channel=None;self.closed=False;self.handled=False
        self.barriers=OrderedBarrier(collector,now);self.probe_token=None;self.retirement_status=None
        self.frame_seq=0;self.frame_deadline=None;self.outbound=None;self.after_send=None
        self.last_hook_ok=False
        self.owned_leases=[]
        self.probe=probe_factory(self._cleanup_probe)
    def source_current(self, source):
        # Owner does metadata-only FD/name revalidation; full exact reread is
        # performed in the serial bind/retirement probe. No subprocess here.
        ram.require(source is self.source and not self.closed and self.collector.epoch==self.epoch and self.collector.fault is None)
        for held in (source,)+self.p.companions:
            a=os.fstat(held.fd);b=os.stat(held.name,dir_fd=held.directory_fd,follow_symlinks=False)
            ram.require(self.stable.source_stamp(a)==held.stamp==self.stable.source_stamp(b))
            self.held_pin(held,a)
    @staticmethod
    def held_pin(held, info):
        # Rebind the selected pin to THIS live FD and exact selected raw bytes.
        # Neither a trusted constructor nor an arbitrary matching stamp suffices.
        ram.require(type(held) is HeldSourceFD and type(held.pin) is ram.SourcePin
            and stat.S_ISREG(info.st_mode)
            and held.pin == ram.SourcePin(info.st_dev,info.st_ino,hashlib.sha256(held.raw).hexdigest()))
    def full_sources(self):
        for held in (self.source,)+self.p.companions:
            self.stable.checked_source(held.fd,held.directory_fd,held.name,held.raw,held.stamp)
            self.held_pin(held,os.fstat(held.fd))
    def _bind_probe(self, source, channel, deadline):
        self.full_sources();peer=struct.unpack('3i',channel.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        ram.require(peer[1]==self.stable.UID and self.now()<deadline)
        state=self.env.show(self.p.runner,self.p.plan,'healthy',deadline)
        invocation=self.env.owned(state,self.p.plan,'healthy');cgroup=self.p.plan['cgroups']['healthy']
        ram.require(state['MainPID']==str(peer[0]) and state['ControlGroup']==cgroup)
        rc,raw=self.p.runner.run(['/usr/bin/systemctl','--user','show',self.p.plan['units']['healthy'],'--property=ExecStart'],self.env.remaining(deadline,1))
        ram.require(rc==0);self.stable.inspect_exec(raw,' '.join(self.p.exec_argv(int(deadline*1e9))))
        key=self.stable.inspect_process(peer[0],cgroup);pidfd=os.pidfd_open(peer[0],0)
        lease=SourceSession(self,ram.ProcessKey(*key).checked(),pidfd,channel,invocation,cgroup,self.initial)
        self.owned_leases.append(lease) # worker cleanup owns it even after cancellation
        ram.require(self.stable.alive(pidfd) and self.stable.inspect_process(peer[0],cgroup)==key)
        ram.require(os.stat('/proc/self/ns/time').st_ino==os.stat('/proc/'+str(peer[0])+'/ns/time').st_ino)
        state=self.env.show(self.p.runner,self.p.plan,'healthy',deadline)
        ram.require(self.env.owned(state,self.p.plan,'healthy',invocation)==invocation and state['MainPID']==str(peer[0]) and self.now()<deadline)
        self.full_sources();return lease
    def bind_held(self, source, channel, deadline):
        # Consumes only this worker's opaque token; never accepts caller facts.
        token=self.probe_token
        ram.require(type(token) is ProbeResult and token.operation=='bind' and token.error is None)
        lease=token.value;ram.require(type(lease) is SourceSession and lease.manager is self and lease.channel is channel and source is self.source)
        self.probe_token=None;self.lease=lease;self.lease_current(lease);self.source_current(source);return lease
    def lease_current(self, lease):
        ram.require(type(lease) is SourceSession and lease is self.lease and lease.manager is self and lease.epoch==self.epoch
            and self.collector.epoch==self.epoch and self.collector.fault is None and self.stable.alive(lease.pidfd)
            and self.stable.inspect_process(lease.process.pid,lease.cgroup)==(lease.process.pid,lease.process.birth_ticks,lease.process.boot_id))
    def drain_before_open(self, lease, deadline):
        self.lease_current(lease);self.source_current(self.source)
        self.fan.drain(self.group,self.observation)
        ram.require(not self.observation.unknown and not self.observation.records and self.now()<deadline)
    def exact_open(self, lease, source, event):
        self.lease_current(lease);self.source_current(source)
        ram.require(any(r is event for r in self.observation.records) and not self.observation.unknown and len(self.observation.records)==1
            and type(event) is dict and type(event.get('pid')) is int and event['pid']==lease.process.pid
            and (event.get('dev'),event.get('ino'))==(source.pin.dev,source.pin.ino) and event.get('mask',0)&0x20)
    def channel_current(self, lease, channel):
        ram.require(channel is lease.channel)
        peer=struct.unpack('3i',channel.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
        ram.require(peer[0]==lease.process.pid and peer[1]==self.stable.UID)
    def release_once(self, lease, qualified, context, deadline):
        # One nonblocking packet attempt; failure never waits/retries. Bootstrap
        # independently expires the SAME hold and runs original business once.
        if lease is None:
            if self.pending_channel is not None:self.pending_channel.close();self.pending_channel=None
            return
        ram.require(lease is self.lease and not lease.released);lease.released=True
        if self.now()<deadline:
            raw=b'qualified-run' if qualified else b'unqualified-run'
            ram.require(lease.channel.send(raw)==len(raw))
    def _retire_probe(self, lease):
        deadline=self.frame_deadline
        ram.require(not self.stable.alive(lease.pidfd) and self.now()<deadline)
        state=self.env.show(self.p.runner,self.p.plan,'healthy',deadline)
        ram.require(self.env.owned(state,self.p.plan,'healthy',lease.invocation)==lease.invocation and state['MainPID']=='0')
        rc,raw=self.p.runner.run(['/usr/bin/systemctl','--user','show',self.p.plan['units']['healthy'],'--property=ExecMainCode','--property=ExecMainStatus'],self.env.remaining(deadline,1))
        ram.require(rc==0 and len(raw)<=256)
        values=dict(line.split('=',1) for line in raw.decode('ascii').splitlines())
        ram.require(set(values)=={'ExecMainCode','ExecMainStatus'} and values['ExecMainCode']=='1' and values['ExecMainStatus'].isdigit())
        state=self.env.show(self.p.runner,self.p.plan,'healthy',deadline)
        ram.require(self.env.owned(state,self.p.plan,'healthy',lease.invocation)==lease.invocation and state['MainPID']=='0')
        self.full_sources();ram.require(self.now()<deadline);return int(values['ExecMainStatus'])
    def retired(self, lease):
        ram.require(lease is self.lease and self.phase=='retire_apply' and type(self.retirement_status) is int
            and not self.stable.alive(lease.pidfd) and self.collector.fault is None)
        return self.retirement_status
    def barrier(self, deadline):
        # Existing global UNKNOWN stays. No synchronous ListJobs call here and
        # no claim the cached final phase certifies a future window.
        ram.require(self.phase=='done' and not self.closed and self.collector.fault is None and self.now()<deadline)
        self.source_current(self.source);ram.require(not self.observation.unknown and len(self.observation.records)==1)
    def packet(self, deadline):
        ram.require(self.now()<deadline)
        try:raw,_anc,flags,_addr=self.pending_channel.recvmsg(2049,0,socket.MSG_DONTWAIT)
        except BlockingIOError:return None
        ram.require(raw and len(raw)<=2048 and not _anc and not flags&(socket.MSG_TRUNC|socket.MSG_CTRUNC));return raw
    def terminal_tail(self):
        # Refuse observed post-terminal packets; no further ACK/count/replay.
        # Empty/nonready transport is NOT an absence or retirement proof.
        try:raw,anc,flags,_addr=self.pending_channel.recvmsg(2049,0,socket.MSG_DONTWAIT)
        except BlockingIOError:return
        ram.require(not anc and not flags&(socket.MSG_TRUNC|socket.MSG_CTRUNC) and not raw)
    def start_probe(self, operation, function, deadline):
        ram.require(self.now()<deadline)
        self.probe.submit(operation,function);self.frame_deadline=deadline
    def poll_probe(self, operation):
        ram.require(self.now()<self.frame_deadline)
        token=self.probe.poll()
        if token is None:return None
        ram.require(type(token) is ProbeResult and token.operation==operation)
        if token.error is not None:raise token.error
        return token
    def _fault(self, owner, error):
        try:
            owner.fail(self.ticket)
            if self.ticket is not None and not self.ticket.released:owner.release(self.ticket)
        except BaseException as cleanup_error:
            if isinstance(error,Exception) and not isinstance(cleanup_error,Exception):error=cleanup_error
        finally:
            self.phase='failed'
            try:self.barriers.close()
            finally:self.probe.close()
        if not isinstance(error,Exception):raise error
    def step(self, owner):
        if self.closed or self.phase in ('done','failed'):return
        try:self._turn(owner)
        except BaseException as error:self._fault(owner,error)
    def _turn(self, owner):
        # Exactly one transition/action per call. No select(timeout), sleep,
        # subprocess, collector.call/barrier, worker join or exit wait here.
        ram.require(self.now()<self.p.service_deadline and self.collector.fault is None)
        phase=self.phase
        if phase=='initial_begin':
            self.barriers.begin(min(self.p.service_deadline,self.now()+.15));self.phase='initial_poll'
        elif phase=='initial_poll':
            receipt=self.barriers.poll()
            if receipt is not None:self.initial=receipt;self.phase='accept'
        elif phase=='accept':
            channel=self.p.poll_held()
            if channel is None:return
            self.pending_channel=channel;channel.setblocking(False);self.handled=True
            self.ticket=next(iter(owner.active.values()),None) if len(owner.active)==1 else None
            ram.require(self.ticket is not None and self.now()<self.ticket.deadline);self.phase='held'
        elif phase=='held':
            raw=self.packet(self.ticket.deadline)
            if raw is None:return
            ram.require(raw==b'held-before-quota-source')
            self.start_probe('bind',lambda:self._bind_probe(self.source,self.pending_channel,self.ticket.deadline),self.ticket.deadline)
            self.phase='bind_poll'
        elif phase=='bind_poll':
            token=self.poll_probe('bind')
            if token is None:return
            self.probe_token=token
            starts=[r for r in self.events if r['unit']==self.p.plan['units']['healthy']]
            ram.require(len(starts)==1 and starts[0]['member']=='JobNew' and starts[0]['epoch']==self.epoch
                and self.initial['stream_index']<starts[0]['stream_index'])
            ram.require(owner.bind_started(self.ticket,self.pending_channel));self.phase='open_send'
        elif phase=='open_send':
            raw=b'open:'+self.ticket.context.encode('ascii')
            ram.require(self.now()<self.ticket.deadline and self.pending_channel.send(raw)==len(raw));self.phase='open_receive'
        elif phase=='open_receive':
            raw=self.packet(self.ticket.deadline)
            if raw is None:return
            ram.require(raw==b'quota-source-opened');self.phase='open_apply'
        elif phase=='open_apply':
            self.fan.drain(self.group,self.observation)
            ram.require(len(self.observation.records)==1 and owner.source_open(self.ticket,self.observation.records[0]));self.phase='release'
        elif phase=='release':
            ram.require(owner.release(self.ticket));self.phase='hook_receive'
        elif phase=='hook_receive':
            ram.require(self.frame_seq<MAX_QUOTA_MONITOR_FRAMES)
            raw=self.packet(self.p.service_deadline)
            if raw is None:return
            self.frame_deadline=min(self.p.service_deadline,self.now()+.15)
            self.fan.drain(self.group,self.observation)
            ram.require(not self.observation.unknown and len(self.observation.records)==1)
            request=decode(raw,FRAME)
            ram.require(type(request) is dict and set(request)=={'v','op','frame'} and type(request['v']) is int and request['v']==1 and request['op']=='hook')
            reply=owner.hook(self.ticket,self.pending_channel,request['frame'])
            self.last_hook_ok=reply.get('ok') is True
            self.outbound=json.dumps(reply,separators=(',',':')).encode();self.phase='hook_send'
        elif phase=='hook_send':
            ram.require(self.now()<self.frame_deadline and self.pending_channel.send(self.outbound)==len(self.outbound));self.outbound=None
            self.frame_seq+=1
            ram.require(self.last_hook_ok)
            if self.ticket.terminal:self.phase='exit_poll'
            else:
                ram.require(self.frame_seq<MAX_QUOTA_MONITOR_FRAMES)
                self.phase='hook_receive'
        elif phase=='exit_poll':
            self.terminal_tail()
            if self.stable.alive(self.lease.pidfd):return
            end=min(self.p.service_deadline,self.now()+.15)
            self.start_probe('retire',lambda:self._retire_probe(self.lease),end);self.phase='retire_poll'
        elif phase=='retire_poll':
            token=self.poll_probe('retire')
            if token is None:return
            self.retirement_status=token.value;self.phase='final_begin'
        elif phase=='final_begin':
            self.barriers.begin(min(self.frame_deadline,self.p.service_deadline));self.phase='final_poll'
        elif phase=='final_poll':
            receipt=self.barriers.poll()
            if receipt is None:return
            rows=[r for r in self.events if r['unit']==self.p.plan['units']['healthy']]
            expected='done' if self.retirement_status==0 else 'failed'
            ram.require(len(rows)==2 and rows[0]['member']=='JobNew' and rows[1]['member']=='JobRemoved'
                and rows[0]['job_id']==rows[1]['job_id'] and rows[1]['result']==expected
                and all(r['epoch']==self.epoch for r in rows) and self.initial['stream_index']<rows[0]['stream_index']<rows[1]['stream_index']<receipt['stream_index'])
            self.fan.drain(self.group,self.observation)
            ram.require(not self.observation.unknown and len(self.observation.records)==1);self.phase='retire_apply'
        elif phase=='retire_apply':
            self.terminal_tail()
            owner.retire(self.ticket);self.phase='done'
        else:raise ram.Unknown('phase_unqualified')
    def _cleanup_probe(self):
        # Worker alone performs potentially blocking ownership-fenced own-unit
        # cleanup. Borrowed prepared FDs remain caller-owned until probe.done.
        try:self.p.cleanup_owned()
        finally:
            first=None
            for lease in self.owned_leases:
                try:os.close(lease.pidfd)
                except BaseException as error:
                    if first is None:first=error
                try:lease.channel.close()
                except BaseException as error:
                    if first is None:first=error
            if self.pending_channel is not None and not self.owned_leases:
                try:self.pending_channel.close()
                except BaseException as error:
                    if first is None:first=error
            if first is not None:raise first
    def close(self):
        self.closed=True;self.barriers.close();self.probe.close()


class SubprocessRunner:
    """Bounded subprocess runner without shell; strips observer env."""

    def run(self, argv, timeout):
        ram.require(type(argv) in (list, tuple) and len(argv) > 0 and all(type(a) is str for a in argv))
        ram.require(type(timeout) in (int, float) and math.isfinite(timeout) and 0 < timeout <= 15)
        env = {k: v for k, v in os.environ.items() if not k.startswith('HUB_ALERT_')}
        result = subprocess.run(list(argv), capture_output=True, timeout=timeout, check=False, env=env)
        return result.returncode, result.stdout


class StableRuntime:
    """OS/source/process verification helpers for V4QuotaManager."""

    def __init__(self, *, uid=None):
        self.UID = os.getuid() if uid is None else uid

    @staticmethod
    def source_stamp(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def checked_source(self, fd, directory_fd, name, raw, stamp):
        a = os.fstat(fd)
        b = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        ram.require(stat.S_ISREG(a.st_mode) and a.st_uid in (0, self.UID)
                    and not (a.st_mode & 0o022) and a.st_nlink == 1)
        ram.require(self.source_stamp(a) == stamp == self.source_stamp(b))
        actual = os.pread(fd, len(raw) + 1, 0)
        ram.require(actual == raw)

    @staticmethod
    def inspect_exec(raw, expected_exec):
        ram.require(type(raw) is bytes and 0 < len(raw) <= 8192 and type(expected_exec) is str and len(expected_exec) > 0)
        text = raw.decode('utf-8', 'strict').strip()
        ram.require('\n' not in text and text.startswith('ExecStart='))
        body = text[len('ExecStart='):].strip()
        if body.startswith('{') and 'argv[]=' in body:
            part = body.split('argv[]=', 1)[1]
            actual_exec = part.split(' ; ', 1)[0].strip()
        else:
            actual_exec = body
        ram.require(actual_exec == expected_exec)

    @staticmethod
    def inspect_process(pid, cgroup):
        ram.require(type(pid) is int and pid > 0 and type(cgroup) is str and cgroup.startswith('/'))
        with open(f'/proc/{pid}/stat', 'rb') as f:
            raw = f.read(8193)
        ram.require(len(raw) <= 8192)
        birth = int(raw[raw.rindex(b')') + 2:].split()[19])
        with open('/proc/sys/kernel/random/boot_id', 'r', encoding='ascii') as f:
            boot_id = f.read(64).strip()
        with open(f'/proc/{pid}/cgroup', 'r', encoding='utf-8') as f:
            cgroups = f.read(4097)
        ram.require(len(cgroups) <= 4096)
        ram.require(any(line.split(':', 2)[-1] == cgroup for line in cgroups.splitlines() if ':' in line))
        ram.ProcessKey(pid, birth, boot_id).checked()
        return (pid, birth, boot_id)

    @staticmethod
    def alive(pidfd):
        ram.require(type(pidfd) is int and pidfd >= 0)
        ready, _, _ = select.select([pidfd], [], [], 0)
        return not ready


class UserManagerEnvironment:
    """USER-systemd manager state/ownership queries via SubprocessRunner."""

    def __init__(self, *, now=time.monotonic):
        self.now = now

    def remaining(self, deadline, cap=1.0):
        left = deadline - self.now()
        ram.require(left > 0)
        return min(cap, left)

    def show(self, runner, plan, role, deadline):
        unit = plan['units'][role]
        rc, raw = runner.run(
            ['/usr/bin/systemctl', '--user', 'show', unit,
             '--property=MainPID,ControlGroup,InvocationID,ActiveState,SubState'],
            self.remaining(deadline, 1.0),
        )
        ram.require(rc == 0 and 0 < len(raw) <= 4096)
        values = {}
        for line in raw.decode('utf-8', 'strict').splitlines():
            ram.require('=' in line)
            key, val = line.split('=', 1)
            values[key] = val
        ram.require({'MainPID', 'ControlGroup', 'InvocationID'} <= set(values))
        return values

    @staticmethod
    def owned(state, plan, role, invocation=None):
        ram.require(type(state) is dict)
        inv = state.get('InvocationID', '')
        ram.require(ram.identity(inv) and state.get('ControlGroup') == plan['cgroups'][role])
        if invocation is not None:
            ram.require(inv == invocation)
        return inv


class FanotifyObservation:
    """Mutable observation buffer drained on the owner thread."""

    def __init__(self):
        self.records = []
        self.unknown = False


class FanotifyReader:
    """Bounded non-blocking reader for an owned Linux fanotify group FD."""

    _STRUCT = struct.Struct('<IBBHqii')
    _VERS = 3
    _FAN_NOFD = -1
    _FAN_Q_OVERFLOW = 0x00004000
    _FAN_OPEN = 0x00000020
    _FAN_CLOEXEC = 0x00000001
    _FAN_NONBLOCK = 0x00000002
    _FAN_MARK_ADD = 0x00000001
    MAX_READS = 16
    MAX_RECORDS = 64
    BUDGET_S = 0.05

    @classmethod
    def open_group(cls, held_sources):
        """Attempt to initialize a real fanotify group; fails closed on EPERM."""
        libc = ctypes.CDLL('libc.so.6', use_errno=True)
        libc.fanotify_init.argtypes = [ctypes.c_uint, ctypes.c_uint]
        libc.fanotify_init.restype = ctypes.c_int
        libc.fanotify_mark.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.c_uint64, ctypes.c_int, ctypes.c_char_p]
        libc.fanotify_mark.restype = ctypes.c_int
        group = libc.fanotify_init(cls._FAN_CLOEXEC | cls._FAN_NONBLOCK, os.O_RDONLY | os.O_CLOEXEC)
        if group < 0:
            raise ram.Unknown('fanotify_unavailable')
        try:
            for held in held_sources:
                rc = libc.fanotify_mark(group, cls._FAN_MARK_ADD, cls._FAN_OPEN, held.directory_fd, held.name.encode())
                if rc < 0:
                    raise ram.Unknown('fanotify_mark_failed')
            return group
        except BaseException:
            os.close(group)
            raise

    @classmethod
    def _close_remaining(cls, buf, offset):
        """Close every valid event FD in the remainder of an already-read buffer."""
        while offset + cls._STRUCT.size <= len(buf):
            event_len, vers, _res, meta_len, _mask, fd, _pid = cls._STRUCT.unpack_from(buf, offset)
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if vers != cls._VERS or meta_len < cls._STRUCT.size or event_len < meta_len or offset + event_len > len(buf):
                break
            offset += event_len

    def drain(self, group, observation):
        observation.records = []
        observation.unknown = False
        deadline = time.monotonic() + self.BUDGET_S
        reads = 0
        try:
            while reads < self.MAX_READS and time.monotonic() < deadline:
                try:
                    buf = os.read(group, 4096)
                except BlockingIOError:
                    return
                if not buf:
                    return
                reads += 1
                offset = 0
                while offset + self._STRUCT.size <= len(buf):
                    event_len, vers, _res, meta_len, mask, fd, pid = self._STRUCT.unpack_from(buf, offset)
                    if (vers != self._VERS or meta_len < self._STRUCT.size
                            or event_len < meta_len or offset + event_len > len(buf)
                            or (mask & self._FAN_Q_OVERFLOW) or fd < 0 or pid <= 0
                            or len(observation.records) >= self.MAX_RECORDS):
                        observation.unknown = True
                        observation.records = []
                        self._close_remaining(buf, offset)
                        return
                    try:
                        info = os.fstat(fd)
                        observation.records.append({
                            'pid': pid, 'dev': info.st_dev, 'ino': info.st_ino, 'mask': int(mask),
                        })
                    except BaseException:
                        observation.unknown = True
                        observation.records = []
                        self._close_remaining(buf, offset + event_len)
                        return
                    finally:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    offset += event_len
                if offset != len(buf):
                    observation.unknown = True
                    observation.records = []
                    return
            # Exhausted read/time budget before EAGAIN: fail closed as unknown.
            observation.unknown = True
            observation.records = []
        except BaseException:
            observation.unknown = True
            observation.records = []


def hold_source_fd(directory, name, *, expected_sha256=None):
    """Open and pin a selected immutable source file via dir_fd + O_NOFOLLOW."""
    ram.require(type(name) is str and '/' not in name and name not in ('', '.', '..'))
    uid = os.getuid()
    dir_fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    fd = -1
    try:
        dstat = os.fstat(dir_fd)
        ram.require(stat.S_ISDIR(dstat.st_mode) and dstat.st_uid in (0, uid) and not (dstat.st_mode & 0o002))
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)
        info = os.fstat(fd)
        ram.require(stat.S_ISREG(info.st_mode) and info.st_uid in (0, uid)
                    and info.st_nlink == 1 and not (info.st_mode & 0o222)
                    and 0 < info.st_size <= 262144)
        raw = os.pread(fd, info.st_size + 1, 0)
        ram.require(len(raw) == info.st_size)
        digest = hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None:
            ram.require(digest == expected_sha256)
        stamp = StableRuntime.source_stamp(info)
        pin = ram.SourcePin(info.st_dev, info.st_ino, digest).checked()
        return HeldSourceFD(fd, dir_fd, name, raw, stamp, pin)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        os.close(dir_fd)
        raise


def close_held_source(held):
    """Close held file and directory descriptors safely."""
    for fd in (held.fd, held.directory_fd):
        try:
            os.close(fd)
        except OSError:
            pass


class SourceGate:
    """Private owned AF_UNIX SEQPACKET listener for source-gate.sock."""

    def __init__(self, directory):
        from pathlib import Path
        from .alert_broker import private_directory
        self.directory = Path(directory)
        self.dir_fd = private_directory(self.directory)
        self.listener = None
        self.node_identity = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
            self.listener = sock
            sock.bind(f'/proc/self/fd/{self.dir_fd}/source-gate.sock')
            node = os.stat('source-gate.sock', dir_fd=self.dir_fd, follow_symlinks=False)
            ram.require(stat.S_ISSOCK(node.st_mode) and node.st_uid == os.getuid() and node.st_nlink == 1)
            self.node_identity = (node.st_dev, node.st_ino)
            os.chmod(f'/proc/self/fd/{self.dir_fd}/source-gate.sock', 0o600)
            sock.setblocking(False)
            sock.listen(1)
        except BaseException:
            self.cleanup()
            raise

    def poll_held(self):
        if self.listener is None:
            return None
        try:
            conn, _ = self.listener.accept()
            return conn
        except BlockingIOError:
            return None

    def cleanup(self):
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
            self.listener = None
        if self.node_identity is not None and self.dir_fd is not None:
            try:
                node = os.stat('source-gate.sock', dir_fd=self.dir_fd, follow_symlinks=False)
                if (node.st_dev, node.st_ino) == self.node_identity:
                    os.unlink('source-gate.sock', dir_fd=self.dir_fd)
            except OSError:
                pass
            self.node_identity = None
        if self.dir_fd is not None:
            try:
                os.close(self.dir_fd)
            except OSError:
                pass
            self.dir_fd = None


def prepare_selected_quota(
    *,
    source,
    companions,
    gate,
    group,
    plan,
    exec_argv,
    service_deadline,
    stable=None,
    manager_environment=None,
    fan=None,
    observation=None,
    runner=None,
    extra_cleanup=None,
):
    """Construct PreparedQuotaSelection from already-held FDs and owned gate."""
    def _cleanup():
        first = None
        if extra_cleanup is not None:
            try:
                extra_cleanup()
            except BaseException as err:
                first = err
        try:
            gate.cleanup()
        except BaseException as err:
            if first is None:
                first = err
        for held in (source,) + tuple(companions):
            close_held_source(held)
        try:
            os.close(group)
        except OSError:
            pass
        if first is not None:
            raise first

    return PreparedQuotaSelection(
        stable=stable or StableRuntime(),
        manager_environment=manager_environment or UserManagerEnvironment(),
        fan=fan or FanotifyReader(),
        group=group,
        observation=observation or FanotifyObservation(),
        source=source,
        companions=tuple(companions),
        runner=runner or SubprocessRunner(),
        plan=plan,
        exec_argv=exec_argv,
        poll_held=gate.poll_held,
        cleanup_owned=_cleanup,
        service_deadline=service_deadline,
    )
