"""INERT public-loopback fixture adapter; native effects fake, transport real.

Entry gets one bounded private config line, then stop/disconnect controls ONLY.
No source method, provider, launcher, HTTP response or delivery is fabricated.
"""
import json
import logging
import os
from pathlib import Path
import re
import select
import signal
import stat
import sys
import time
from functools import partial
from contextlib import contextmanager
from urllib.parse import quote, urlencode, urlsplit
import uuid

from agent_hub_host.client import Client
from agent_hub_host import messages
from native_mail_restart_fixture import (FixtureViolation, NoWorkPool,
    StopServe, make_fixture_bridge, require)
import threading

CONFIG_KEYS = {'origin','token','directory','mode','machine_id','binding',
               'recipient','first_body','later_body','remaining_ms'}
BINDING_KEYS = {'session_id','native_id','operation_id','epoch','workspace','tag'}


def decode(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result); result[key] = value
        return result
    try: return json.loads(raw, object_pairs_hook=pairs,
                           parse_constant=lambda _: require(False))
    except (ValueError, TypeError): raise FixtureViolation('invalid_json') from None


def identifier(value, maximum=180):
    return isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,'+str(maximum)+'}',value) is not None


def config(value):
    require(type(value) is dict and set(value) == CONFIG_KEYS)
    require(type(value['remaining_ms']) is int and 1 <= value['remaining_ms'] <= 35000)
    origin = value['origin']
    require(isinstance(origin,str) and re.fullmatch(r'http://127\.0\.0\.1:[1-9][0-9]{0,4}',origin))
    parsed = urlsplit(origin)
    try: port=parsed.port
    except ValueError: raise FixtureViolation('invalid_origin') from None
    require(port is not None and 1 <= port <= 65535)
    require(value['mode'] in ('crash','receive') and identifier(value['machine_id'],80))
    token = value['token']
    require(isinstance(token,str) and 1 <= len(token) <= 4096 and all(33 <= ord(c) <= 126 for c in token))
    binding = value['binding']
    require(type(binding) is dict and set(binding) == BINDING_KEYS)
    for key in ('session_id','native_id','operation_id','tag'): require(identifier(binding[key],120))
    try: canonical_native=str(uuid.UUID(binding['native_id']))
    except (ValueError,TypeError,AttributeError): raise FixtureViolation('invalid_native_identity') from None
    require(canonical_native == binding['native_id'])
    require(type(binding['epoch']) is int and 1 <= binding['epoch'] <= 9007199254740991)
    for name in ('directory',):
        require(isinstance(value[name],str) and os.path.isabs(value[name]) and '\x00' not in value[name])
    require(isinstance(binding['workspace'],str) and os.path.isabs(binding['workspace']) and '\x00' not in binding['workspace'])
    require(isinstance(value['recipient'],str) and re.fullmatch(r'[A-Za-z0-9_.:@/-]{1,180}',value['recipient']))
    for name in ('first_body','later_body'):
        require(isinstance(value[name],str) and 1 <= len(value[name].encode()) <= 2048)
    require(value['first_body'] != value['later_body'])
    return value


class Budget:
    def __init__(self, milliseconds): self.end = time.monotonic()+milliseconds/1000
    def left(self, cap=1):
        remaining = self.end-time.monotonic()
        require(remaining > 0); return min(cap,remaining)


class Channel:
    def __init__(self, budget):
        self.budget, self.buffer, self.total, self.written = budget, b'', 0, 0
        os.set_blocking(0,False); os.set_blocking(1,False)
    def line(self, wait):
        if b'\n' not in self.buffer:
            if not select.select([0],[],[],self.budget.left(wait))[0]: return None
            raw = os.read(0,8193); require(bool(raw))
            self.total += len(raw); require(self.total <= 16384 and len(self.buffer)+len(raw) <= 8192)
            self.buffer += raw
        if b'\n' not in self.buffer: return None
        line,self.buffer = self.buffer.split(b'\n',1)
        return decode(line)
    def observe(self, kind, **fields):
        raw = json.dumps({'kind':kind, **fields},separators=(',',':')).encode()+b'\n'
        self.written += len(raw); require(len(raw)<=2048 and self.written<=65536)
        while raw:
            require(select.select([],[1],[],self.budget.left(.15))[1])
            count=os.write(1,raw); require(count>0); raw=raw[count:]
    def control(self):
        value=self.line(0)
        if value is None: return
        require(value in ({'control':'stop'},{'control':'disconnect'}))
        if value['control']=='stop': raise StopServe()
        raise ConnectionResetError('synthetic_disconnect')


def private_root(path):
    root=Path(path); meta=root.lstat()
    require(root.resolve()==root and stat.S_ISDIR(meta.st_mode) and meta.st_uid==os.getuid()
            and stat.S_IMODE(meta.st_mode)==0o700)
    return root


def durable_json(path, value):
    # Fresh exclusively-owned scratch; no foreign file overwrite or repair.
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(value,stream,sort_keys=True); stream.flush(); os.fsync(stream.fileno())
    directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)


def read_json(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'rb') as stream:
        meta=os.fstat(stream.fileno())
        require(stat.S_ISREG(meta.st_mode) and meta.st_uid==os.getuid() and meta.st_nlink==1
                and not meta.st_mode&0o077 and meta.st_size<=8192)
        raw=stream.read(8193); require(len(raw)<=8192)
    return decode(raw)


class Scope:
    def __init__(self, value, root, channel):
        self.value,self.root,self.channel=value,root,channel
        self.rows={}
        for kind in ('first','later'):
            path=root/(kind+'.observed.json')
            if path.exists():
                row=read_json(path); self.check(row)
                require(row['body']==value[kind+'_body']); self.rows[kind]=row
        if value['mode']=='crash': require(not self.rows)
        else: require('first' in self.rows and 'later' not in self.rows)
    def check(self,row):
        require(type(row) is dict and set(row)=={'id','epoch','recipient','session_id','operation_id','body'})
        require(identifier(row['id']) and identifier(row['operation_id']))
        require(type(row['epoch']) is int and 1<=row['epoch']<=9007199254740991)
        require(row['session_id']==self.value['binding']['session_id'] and row['recipient']==self.value['recipient'])
        require(row['body'] in (self.value['first_body'],self.value['later_body']))
    def receive(self,row):
        self.check(row)
        kind='first' if row['body']==self.value['first_body'] else 'later'
        require(kind=='first' or (self.value['mode']=='receive' and 'first' in self.rows))
        if kind in self.rows: require(row==self.rows[kind])
        else:
            require(all(row['id']!=other['id'] for other in self.rows.values()))
            durable_json(self.root/(kind+'.observed.json'),row); self.rows[kind]=row
        self.channel.observe('received',message_id=row['id'],which=kind)
    def effect(self, identity, body):
        matches=[kind for kind,row in self.rows.items() if row['id']==identity and row['body']==body]
        require(len(matches)==1)
        require(matches[0]==('first' if self.value['mode']=='crash' else 'later'))
    def api(self, method,path,body,key):
        require(method=='POST' and type(body) is dict)
        matches=[(kind,row) for kind,row in self.rows.items() if path=='/api/messages/'+quote(row['id'],safe='')+'/delivered?'+urlencode({'mailbox':row['recipient']})]
        require(len(matches)==1)
        kind,row=matches[0]
        require(type(body.get('epoch')) is int and body['epoch']==row['epoch']
                and body.get('operation_id')==row['operation_id'])
        if body.get('phase')=='claim': require(set(body)=={'phase','epoch','operation_id'} and key is None)
        else:
            require(kind=='later' and body.get('phase')=='receipt'
                    and set(body)=={'phase','epoch','operation_id','claim_id','native_receipt'}
                    and key=='mail:'+row['id']+':'+str(row['epoch']))
            receipt=body['native_receipt']
            require(type(receipt) is dict and set(receipt)=={'provider','message_id','session_id','recipient'}
                    and receipt['provider']=='aplexer' and receipt['session_id']==row['session_id']
                    and receipt['recipient']==row['recipient'])
            try: canonical=str(uuid.UUID(receipt['message_id']))
            except (ValueError,TypeError,AttributeError): raise FixtureViolation('invalid_receipt') from None
            require(canonical==receipt['message_id'])
        return kind,row


class RetryTelemetry:
    """Diagnostic-only fixed metadata; failed/dropped records convey UNKNOWN.

    No retries or positive evidence. At most 16 records / 4096 bytes, single
    nonblocking writes, bounded by the existing absolute fixture deadline.
    """
    def __init__(self, budget):
        self.budget = budget
        self.generation = 0
        self.failure = None
        self.records = self.bytes = 0
        self.unknown = False
    @contextmanager
    def at(self, stage):
        try:
            yield
        except Exception as error:
            # A normal receive poll is handled by Bridge, not a disconnect.
            if not (stage == 'receive' and isinstance(error, TimeoutError)):
                self.failure = (error, stage)
            raise
    def connecting(self):
        self.generation += 1
        self.failure = None
    def retry(self):
        error = sys.exc_info()[1]
        stage = 'bridge_unknown'
        if self.failure is not None and self.failure[0] is error:
            stage = self.failure[1]
        self.failure = None
        classes = {TimeoutError:'TIMEOUT', ConnectionResetError:'CONNECTION_RESET',
                   ConnectionError:'CONNECTION', OSError:'OS', RuntimeError:'RUNTIME',
                   ValueError:'VALUE', TypeError:'TYPE'}
        label = classes.get(type(error), 'OTHER')
        row = {'kind':'retry_failure', 'connection_generation':self.generation,
               'stage':stage, 'exception_class':label}
        try:
            if self.unknown or self.records >= 16 or self.bytes >= 4096:
                self.unknown = True; return
            self.budget.left(0)
            raw = json.dumps(row, separators=(',',':')).encode()+b'\n'
            if len(raw)>512 or self.bytes+len(raw)>4096:
                self.unknown=True; return
            # stderr is private and nonblocking; never wait for capture drainage.
            if not select.select([], [2], [], 0)[1]:
                self.unknown=True; return
            self.records += 1; self.bytes += len(raw)
            if os.write(2, raw) != len(raw): self.unknown=True
        except Exception:
            self.unknown=True


class RetryLogging:
    """Intercept only Bridge's exact existing disconnect call, not log payloads."""
    def __init__(self, original, telemetry):
        self.original, self.telemetry = original, telemetry
    def getLogger(self, name):
        if name == 'agent_hub_host.agent': return self
        return self.original.getLogger(name)
    def __getattr__(self, name):
        return getattr(self.original, name)
    def exception(self, message, *args, **kwargs):
        if message == 'Host disconnected machine=%s' and len(args)==1 and not kwargs:
            self.telemetry.retry()
        # All original general logging remains globally disabled.


class ObservedSocket:
    def __init__(self, raw, owner): self.raw,self.owner=raw,owner
    def __enter__(self):
        with self.owner.telemetry.at('connect'):
            self.raw.__enter__()
        try: self.owner.channel.observe('connected'); return self
        except BaseException:
            self.raw.__exit__(None,None,None); raise
    def __exit__(self,*args):
        try: return self.raw.__exit__(*args)
        finally: self.owner.channel.observe('closed')
    def recv(self, timeout=None):
        self.owner.channel.control()
        with self.owner.telemetry.at('receive'):
            raw=self.raw.recv(timeout=self.owner.budget.left(min(timeout or .1,.1)))
        require(isinstance(raw,str) and len(raw.encode())<=16384)
        event=decode(raw)
        if event=={'type':'resync','reason':'connected'}: return raw
        require(type(event) is dict and set(event)=={'type','message'} and event['type']=='mailbox.deliver')
        self.owner.scope.receive(event['message'])
        return raw
    def send(self, raw):
        self.owner.budget.left()
        frame=decode(raw)
        require(type(frame) is dict and frame.get('type')=='host.error'
                and frame.get('reason')=='native_mail_outcome_uncertain'
                and frame.get('session_id')==self.owner.scope.value['binding']['session_id'])
        first=self.owner.scope.rows.get('first',{})
        require(frame.get('message_id')==first.get('id') and frame.get('operation_id')==first.get('operation_id'))
        with self.owner.telemetry.at('send'):
            self.raw.send(raw)
        self.owner.budget.left()
        self.owner.channel.observe('sent_error',message_id=first['id'],reason='native_mail_outcome_uncertain')


class PublicClient(Client):
    def __init__(self,value,budget,channel,scope):
        super().__init__(value['origin'],value['token'],timeout=budget.left())
        self.budget,self.channel,self.scope=budget,channel,scope
        self.telemetry=RetryTelemetry(budget)
    def request(self,method,path,body=None,key=None,timeout=None):
        kind,row=self.scope.api(method,path,body,key)
        with self.telemetry.at('receipt_request' if body['phase']=='receipt' else 'claim_request'):
            result=super().request(method,path,body,key,timeout=self.budget.left(min(timeout or 1,1)))
        self.budget.left()
        if body['phase']=='receipt':
            self.channel.observe('receipt_posted',message_id=row['id'],which=kind,
                                 native_receipt=body['native_receipt'])
        return result
    def socket(self,machine_id,safety=False):
        require(machine_id==self.scope.value['machine_id'] and safety is False)
        self.timeout=self.budget.left()
        self.telemetry.connecting()
        with self.telemetry.at('connect'):
            raw=super().socket(machine_id,safety)
        return ObservedSocket(raw,self)


def run(value,channel,budget):
    value=config(value); root=private_root(value['directory'])
    # Never inherit real config/auth/worker/proxy selectors; token only stdin.
    keep={key:os.environ[key] for key in ('AGENT_HUB_HEAVY_TEST_HELD',) if key in os.environ}
    os.environ.clear(); os.environ.update(keep,HOME=str(root),TMPDIR=str(root),PATH=os.defpath)
    logging.disable(logging.CRITICAL)
    if value['mode']=='crash': require(not (root/'journal.sqlite').exists())
    scope=Scope(value,root,channel); client=PublicClient(value,budget,channel,scope)
    bridge=make_fixture_bridge(root,client,value['mode'],value['binding'],scope.effect)
    journal=bridge.journal; old=messages.deliver
    from agent_hub_host import agent as bridge_module
    old_logging=bridge_module.logging
    try:
        os.set_blocking(2,False)
        bridge_module.logging=RetryLogging(old_logging,client.telemetry)
        binding=value['binding']; identity_path=root/'binding.json'
        if value['mode']=='crash':
            durable_json(identity_path,binding)
            with journal.db:
                journal.db.execute('INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state) VALUES(?,?,?,?,?,?,?,?)',
                    (binding['operation_id'],binding['session_id'],binding['epoch'],'synthetic',binding['workspace'],binding['tag'],binding['native_id'],'running'))
        else:
            require(read_json(identity_path)==binding)
            row=journal.db.execute('SELECT * FROM launches WHERE operation_id=?',(binding['operation_id'],)).fetchone()
            require(row is not None and all(row[k]==binding[k] for k in ('operation_id','session_id','epoch','workspace','tag','native_id')) and row['state']=='running')
            held=journal.db.execute('SELECT * FROM native_mail WHERE id=? AND epoch=?',(scope.rows['first']['id'],scope.rows['first']['epoch'])).fetchone()
            require(held is not None and held['state']=='executing' and held['receipt'] is None)
        bridge.heartbeat=lambda *_:None; bridge.reconcile=lambda:None
        bridge.publish_managed_readiness=lambda *_:None
        bridge._launch_pool=bridge._command_pool=NoWorkPool()
        bridge._worker_slots=threading.BoundedSemaphore(1)
        messages.deliver=partial(old,runner=bridge.fixture_native)
        bridge._serve(value['machine_id'],[])
    except StopServe: return 0
    finally:
        bridge_module.logging=old_logging
        messages.deliver=old; journal.close_thread()


def main():
    # Initial input shares a <=2s bound with all subsequent controls; no argv config.
    require(len(sys.argv)==1)
    started=time.monotonic()
    budget=Budget(2000); channel=Channel(budget)
    value=None
    while value is None: value=channel.line(1)
    value=config(value); budget.end=started+value['remaining_ms']/1000
    previous=signal.getsignal(signal.SIGALRM)
    def expired(*_): raise FixtureViolation('fixture_deadline')
    signal.signal(signal.SIGALRM,expired)
    signal.setitimer(signal.ITIMER_REAL,budget.left(35))
    try: return run(value,channel,budget)
    finally:
        signal.setitimer(signal.ITIMER_REAL,0); signal.signal(signal.SIGALRM,previous)


if __name__=='__main__':
    try: result=main()
    except (FixtureViolation,Exception,KeyboardInterrupt):
        # No error.message/traceback/config/token reaches retained output.
        os.write(2,b'public_fixture_refused\n'); raise SystemExit(2)
    else: raise SystemExit(result)
