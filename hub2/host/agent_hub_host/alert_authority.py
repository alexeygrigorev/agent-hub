"""Inert RAM/refusal core; no kernel/socket/service adapter and NO complete authority.

Only a trusted dispatcher/collector may call admission methods. Producer API-v1
frames never create admissions. No files are loaded as a healthy RAM epoch.
"""
from dataclasses import dataclass
import re
import time
import uuid

ROLES={'canary':'start_canary','quota_publish':'quota_stale','quota_monitor':'quota_stale',
       'backup_pull':'backup','backup_notifier':'backup'}
PRODUCERS=frozenset(ROLES.values())
DECISIONS={'observation':False,'alert':True,'alert_dedup':True,'recovery':True}
MAX_ACTIVE=128
MAX_LIFETIME=4096
ACK_NS=150_000_000

class Unknown(Exception):pass

def require(value):
    if not value:raise Unknown('live_authority_unknown')

def identity(value):return type(value) is str and re.fullmatch('[0-9a-f]{32}',value) is not None

@dataclass(frozen=True)
class ProcessKey:
    pid:int
    birth_ticks:int
    boot_id:str
    def checked(self):
        require(type(self.pid) is int and self.pid>0 and type(self.birth_ticks) is int and self.birth_ticks>0
                and type(self.boot_id) is str and re.fullmatch('[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',self.boot_id))
        return self

@dataclass(frozen=True)
class SourcePin:
    dev:int
    ino:int
    sha256:str
    def checked(self):
        require(type(self.dev) is int and self.dev>=0 and type(self.ino) is int and self.ino>0
                and type(self.sha256) is str and re.fullmatch('[0-9a-f]{64}',self.sha256))
        return self

@dataclass
class Attempt:
    role:str
    parent_id:str|None
    process:ProcessKey
    next_seq:int=0
    began:bool=False
    decisions:int=0
    terminal:bool=False


class LiveRAM:
    """Bounded single-owner event-loop state, NEVER restored from persisted zero.

    The component's output is deliberately complete=false until missing real
    collector/admission surfaces are integrated and independently qualified.
    ram_consistent describes only this core's mathematical state, NOT coverage.
    """
    def __init__(self,source_pins,checked_frame,*,now=time.monotonic_ns):
        require(set(source_pins)==set(ROLES) and callable(checked_frame))
        self.pins={role:pin.checked() for role,pin in source_pins.items()}
        self.checked_frame=checked_frame;self.now=now
        self.epoch=uuid.uuid4().hex;self.last_clock=None
        self.active={};self.seen=set();self.nonces=set()
        self.faults={producer:None for producer in PRODUCERS}
        self.counts={p:{'started':0,'finished':0,'occurrences':0} for p in PRODUCERS}
        self.tick()

    def tick(self):
        value=self.now()
        if type(value) is not int or value<0 or self.last_clock is not None and value<self.last_clock:
            self.poison(PRODUCERS,'clock_unqualified');raise Unknown('clock_unqualified')
        self.last_clock=value;return value

    def poison(self,producers,reason):
        # Reasons are internal fixed literals, never exception strings/content.
        allowed={'clock_unqualified','dispatch_unqualified','replay_or_capacity','parent_unqualified',
                 'process_reused','hook_unqualified','missing_terminal','collector_lost',
                 'storage_lost','source_replaced','unmatched_source_open','barrier_unqualified'}
        try:
            scope=set(producers)
            require(bool(scope) and scope<=PRODUCERS and reason in allowed)
        except Exception:
            scope=PRODUCERS;reason='collector_lost'
        for producer in scope:
            if self.faults[producer] is None:self.faults[producer]=reason

    @staticmethod
    def scope(role):
        return {ROLES[role]} if type(role) is str and role in ROLES else PRODUCERS

    def admit(self,invocation_id,role,parent_id,process,source_pin):
        """Actual trusted dispatcher/manager path ONLY, not a child hook.

        Manager adapters must first distinguish real starts from jobs/coalescing
        and bind the process's boot/birth + exact source. No JobNew-only credit.
        """
        scope=PRODUCERS
        try:
            require(type(role) is str and role in ROLES);p=ROLES[role];scope={p}
            self.tick();process.checked();source_pin.checked()
            require(source_pin==self.pins[role] and identity(invocation_id))
            if invocation_id in self.seen or len(self.seen)>=MAX_LIFETIME or len(self.active)>=MAX_ACTIVE:
                self.poison({p},'replay_or_capacity');raise Unknown('replay_or_capacity')
            if parent_id is not None:
                if not (identity(parent_id) and role=='backup_notifier' and parent_id in self.active
                        and self.active[parent_id].role=='backup_pull'):
                    self.poison({p},'parent_unqualified');raise Unknown('parent_unqualified')
            if any(a.process==process for a in self.active.values()):
                self.poison({p},'process_reused');raise Unknown('process_reused')
            self.seen.add(invocation_id)
            self.active[invocation_id]=Attempt(role,parent_id,process)
            self.counts[p]['started']+=1
        except Exception:
            self.poison(scope,'dispatch_unqualified');raise Unknown('dispatch_unqualified') from None

    def hook(self,frame,channel_id,channel_process):
        """API-v1 checked frame over actual private inherited-FD channel.

        Runs without filesystem/network I/O; transport must ACK within150ms or
        fail observation while original business/mail proceeds exactly once.
        """
        attempt=None;producers=PRODUCERS
        try:
            start=self.tick()
            require(identity(channel_id))
            attempt=self.active.get(channel_id)
            producers={ROLES[attempt.role]} if attempt else PRODUCERS
            frame=self.checked_frame(frame)
            require(attempt is not None and channel_process.checked()==attempt.process
                    and frame['invocation_id']==channel_id and frame['role']==attempt.role
                    and frame['parent_id']==attempt.parent_id and frame['seq']==attempt.next_seq
                    and not attempt.terminal and frame['observer_ok'] is True)
            phase=frame['phase']
            if phase=='begin':
                require(not attempt.began and attempt.next_seq==0);attempt.began=True
            elif phase=='decision':
                require(attempt.began and frame['decision'] in DECISIONS
                        and (attempt.role!='canary' or attempt.decisions==0))
                attempt.decisions+=1;self.counts[ROLES[attempt.role]]['occurrences']+=int(DECISIONS[frame['decision']])
            else:
                require(phase=='terminal' and attempt.began and attempt.decisions>0);attempt.terminal=True
            attempt.next_seq+=1
            require(self.tick()-start<=ACK_NS)
            return {'seq':frame['seq'],'ok':True}
        except Exception:
            self.poison(producers,'hook_unqualified')
            return {'seq':attempt.next_seq if attempt else -1,'ok':False}

    def process_exit(self,invocation_id,process,exit_status):
        attempt=None;scope=PRODUCERS
        try:
            require(identity(invocation_id))
            attempt=self.active.get(invocation_id)
            require(attempt is not None);p=ROLES[attempt.role];scope={p}
            self.tick();require(process.checked()==attempt.process and type(exit_status) is int
                                and attempt.began and attempt.terminal and attempt.decisions>0)
            self.counts[p]['finished']+=1
        except Exception:self.poison(scope,'missing_terminal')
        finally:
            if attempt is not None:self.active.pop(invocation_id,None)

    def source_open(self,role,pin,kernel_pid,verified_process=None):
        """Refusal/correlation only: NO invocation credit from successful opens.

        fanotify has only PID. A collector must already have a still-qualified
        process identity; an after-the-fact PID lookup or ordinary read is NOT
        independent admission. Missing/mismatched identity poisons this role.
        """
        scope=PRODUCERS
        try:
            require(type(role) is str and role in ROLES);scope={ROLES[role]}
            self.tick()
            require(pin.checked()==self.pins[role] and type(kernel_pid) is int
                    and verified_process is not None and verified_process.checked().pid==kernel_pid
                    and any(a.role==role and a.process==verified_process for a in self.active.values()))
        except Exception:self.poison(scope,'unmatched_source_open')

    def source_changed(self,role):self.poison(self.scope(role),'source_replaced')
    def collector_lost(self,producers):self.poison(producers,'collector_lost')
    def storage_lost(self,producers):self.poison(producers,'storage_lost')

    def barrier(self,nonce,drain_current_sources):
        """Runs a REAL adapter drain/barrier, never a clock-only freshness test.

        The adapter is NOT implemented here. It must service same-connection
        manager replies plus all pending kernel/channel events and source checks
        within the call budget, invoking the callbacks on THIS same event loop.
        An unavailable adapter cannot manufacture a healthy snapshot.
        """
        try:
            start=self.tick()
            require(identity(nonce) and nonce not in self.nonces and len(self.nonces)<MAX_LIFETIME)
            self.nonces.add(nonce)
            require(callable(drain_current_sources) and drain_current_sources() is True)
            require(self.tick()-start<=ACK_NS)
        except Exception:self.poison(PRODUCERS,'barrier_unqualified')
        return {'kind':'local_alert_ram_refusal_seam_v1','epoch':self.epoch,'nonce':nonce if identity(nonce) else None,
                'complete':False,'authority':'UNQUALIFIED_DISPATCH_ADAPTERS',
                'ram_consistent':not self.active and not any(self.faults.values()),
                'counts':{p:dict(v) for p,v in self.counts.items()},'faults':dict(self.faults)}
