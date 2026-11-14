"""INERT stdlib/OS-libdbus collector. No connection occurs on import/default main.

One private connection per instance. Ordered manager metadata only, NOT process
admission or complete alert authority. Root prospective test is separate.
"""
import ctypes as C
import hashlib
import json
import math
import os
import socket
import struct
import re
import time
import uuid

LIB='/usr/lib/x86_64-linux-gnu/libdbus-1.so.3.32.4'
LIB_SHA='a6ae7b4ef48562b40d7b9ba8efd2e49f6528b7cc6364bea382dcee4b473b5413'
BUS='org.freedesktop.DBus';BUS_PATH='/org/freedesktop/DBus'
MANAGER='org.freedesktop.systemd1';MANAGER_PATH='/org/freedesktop/systemd1';IFACE=MANAGER+'.Manager'
FIXED={'user':frozenset(('agent-hub-quota-publish.service','agent-hub-quota-monitor.service')),
       'system':frozenset(('hub-start-canary.service','hub-start-canary-full.service'))}
OWNER_RULE="type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',member='NameOwnerChanged',arg0='org.freedesktop.systemd1'"
JOB_RULE="type='signal',sender='org.freedesktop.systemd1',interface='org.freedesktop.systemd1.Manager',path='/org/freedesktop/systemd1'"
MAX_MESSAGES=256;MAX_JOBS=256;MAX_EVENTS=4096;BUDGET_NS=150_000_000

class Unknown(Exception):pass

def require(ok):
    if not ok:raise Unknown('collector_unqualified')

def unique(value):return type(value) is str and re.fullmatch(r':[0-9]+\.[0-9]+',value) is not None

# Public DBusMessageIter ABI, no private library structure. 64-bit Linux only.
class Iter(C.Structure):
    _fields_=[('dummy1',C.c_void_p),('dummy2',C.c_void_p),('dummy3',C.c_uint32),
              *[('dummy'+str(i),C.c_int) for i in range(4,12)],('pad1',C.c_int),('pad2',C.c_void_p),('pad3',C.c_void_p)]
P=C.c_void_p;I=C.POINTER(Iter);U=C.c_uint32
SIGNATURES={
 'dbus_threads_init_default':(U,[]),
 'dbus_connection_open_private':(P,[C.c_char_p,P]),
 'dbus_connection_set_exit_on_disconnect':(None,[P,U]),
 'dbus_connection_set_max_message_size':(None,[P,C.c_long]),
 'dbus_connection_set_max_received_size':(None,[P,C.c_long]),
 'dbus_connection_get_is_connected':(U,[P]),'dbus_connection_get_is_authenticated':(U,[P]),
 'dbus_connection_get_unix_fd':(U,[P,C.POINTER(C.c_int)]),
 'dbus_connection_read_write':(U,[P,C.c_int]),'dbus_connection_pop_message':(P,[P]),
 'dbus_connection_send':(U,[P,P,C.POINTER(U)]),
 'dbus_connection_close':(None,[P]),'dbus_connection_unref':(None,[P]),
 'dbus_message_new_method_call':(P,[C.c_char_p,C.c_char_p,C.c_char_p,C.c_char_p]),
 'dbus_message_unref':(None,[P]),'dbus_message_iter_init_append':(None,[P,I]),
 'dbus_message_iter_append_basic':(U,[I,C.c_int,P]),'dbus_message_iter_init':(U,[P,I]),
 'dbus_message_iter_get_arg_type':(C.c_int,[I]),'dbus_message_iter_get_basic':(None,[I,P]),
 'dbus_message_iter_next':(U,[I]),'dbus_message_iter_recurse':(None,[I,I]),
 'dbus_message_get_type':(C.c_int,[P]),'dbus_message_get_serial':(U,[P]),
 'dbus_message_get_reply_serial':(U,[P]),
 **{name:(C.c_char_p,[P]) for name in ('dbus_message_get_sender','dbus_message_get_interface',
    'dbus_message_get_member','dbus_message_get_path','dbus_message_get_signature')},
}

class LibWire:
    def __init__(self,lib=None,*,peer_uid=None):
        require(C.sizeof(P)==8 and C.sizeof(Iter)==72)
        if lib is None:
            # Only explicit construction loads OS code. Never a library lookup
            # through user PATH/LD_LIBRARY_PATH or a Python third-party package.
            with open(LIB,'rb') as f:require(hashlib.file_digest(f,'sha256').hexdigest()==LIB_SHA)
            lib=C.CDLL(LIB)
        self.c=lib;self.connection=None;self.selected=frozenset();self.peer_uid=peer_uid or self.socket_peer_uid
        for name,(result,args) in SIGNATURES.items():
            fn=getattr(lib,name);fn.restype=result;fn.argtypes=args

    def open(self,kind):
        require(kind in FIXED and self.connection is None)
        require(self.c.dbus_threads_init_default())
        address=b'unix:path=/run/user/1000/bus' if kind=='user' else b'unix:path=/run/dbus/system_bus_socket'
        conn=self.c.dbus_connection_open_private(address,None);require(conn)
        self.connection=conn  # Custody exists before any later operation.
        try:
            self.c.dbus_connection_set_exit_on_disconnect(conn,0)
            self.c.dbus_connection_set_max_message_size(conn,65536)
            self.c.dbus_connection_set_max_received_size(conn,131072)
        except BaseException:self.close();raise

    def close(self):
        conn,self.connection=self.connection,None
        if conn is not None:
            try:self.c.dbus_connection_close(conn)
            finally:self.c.dbus_connection_unref(conn)

    @staticmethod
    def socket_peer_uid(fd):
        # Borrowed dbus transport FD is NEVER read/written/selected/closed.
        # A duplicate is used only for Unix SO_PEERCRED metadata then closed.
        duplicate=os.dup(fd)
        with socket.socket(fileno=duplicate) as peer:
            pid,uid,gid=struct.unpack('3i',peer.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
            require(pid>0)
            return uid

    def peer(self,kind):
        fd=C.c_int(-1)
        require(self.c.dbus_connection_get_is_authenticated(self.connection)
            and self.c.dbus_connection_get_unix_fd(self.connection,C.byref(fd)) and fd.value>=0)
        uid=self.peer_uid(fd.value)
        require(type(uid) is int and uid==(1000 if kind=='user' else 0))

    def send(self,destination,path,interface,member,arg=None):
        message=self.c.dbus_message_new_method_call(destination.encode(),path.encode(),interface.encode(),member.encode());require(message)
        try:
            if arg is not None:
                require(type(arg) is str and len(arg)<1024)
                it=Iter();self.c.dbus_message_iter_init_append(message,C.byref(it));value=C.c_char_p(arg.encode())
                require(self.c.dbus_message_iter_append_basic(C.byref(it),ord('s'),C.byref(value)))
            serial=U();require(self.c.dbus_connection_send(self.connection,message,C.byref(serial)) and serial.value>0)
            return serial.value
        finally:self.c.dbus_message_unref(message)

    @staticmethod
    def text(raw,limit=512):
        if raw is None:return ''
        require(type(raw) is bytes and len(raw)<=limit)
        return raw.decode('utf-8')

    def values(self,it,types):
        out=[]
        for code in types:
            require(self.c.dbus_message_iter_get_arg_type(C.byref(it))==ord(code))
            value=U() if code=='u' else C.c_char_p()
            self.c.dbus_message_iter_get_basic(C.byref(it),C.byref(value))
            out.append(value.value if code=='u' else self.text(value.value,256))
            self.c.dbus_message_iter_next(C.byref(it))
        require(self.c.dbus_message_iter_get_arg_type(C.byref(it))==0)
        return tuple(out)

    def body(self,message,signature):
        # Called ONLY after exact header/signature selection. Never decode an
        # error body, arbitrary signals, properties or unknown generic payload.
        it=Iter();present=self.c.dbus_message_iter_init(message,C.byref(it))
        if signature=='':require(not present);return ()
        require(present)
        if signature in ('s','sss','uos','uoss'):return self.values(it,signature)
        require(signature=='a(usssoo)' and self.c.dbus_message_iter_get_arg_type(C.byref(it))==ord('a'))
        array=Iter();self.c.dbus_message_iter_recurse(C.byref(it),C.byref(array));out=[];count=0
        while self.c.dbus_message_iter_get_arg_type(C.byref(array))!=0:
            count+=1;require(count<=MAX_JOBS and self.c.dbus_message_iter_get_arg_type(C.byref(array))==ord('r'))
            child=Iter();self.c.dbus_message_iter_recurse(C.byref(array),C.byref(child));row=self.values(child,'usssoo')
            if row[1] in self.selected:out.append(row)  # Discard all unrelated job metadata.
            self.c.dbus_message_iter_next(C.byref(array))
        self.c.dbus_message_iter_next(C.byref(it));require(self.c.dbus_message_iter_get_arg_type(C.byref(it))==0)
        return tuple(out)

    def receive(self,timeout_ms,handler):
        require(type(timeout_ms) is int and 0<=timeout_ms<=10 and self.connection is not None)
        require(self.c.dbus_connection_get_is_connected(self.connection))
        require(self.c.dbus_connection_read_write(self.connection,timeout_ms))
        require(self.c.dbus_connection_get_is_connected(self.connection))
        message=self.c.dbus_connection_pop_message(self.connection)
        if not message:return False
        try:
            header={'type':int(self.c.dbus_message_get_type(message)),
                'serial':int(self.c.dbus_message_get_serial(message)),
                'reply_serial':int(self.c.dbus_message_get_reply_serial(message))}
            for field in ('sender','interface','member','path','signature'):
                header[field]=self.text(getattr(self.c,'dbus_message_get_'+field)(message))
            handler(header,lambda sig:self.body(message,sig))
            return True
        finally:self.c.dbus_message_unref(message)

class Collector:
    def __init__(self,wire,kind,selected,on_job,*,test_token=None,now=time.monotonic_ns):
        require(kind in FIXED and callable(on_job) and type(selected) in (set,frozenset,tuple,list))
        selected=frozenset(selected);require(0<len(selected)<=8)
        if test_token is None:require(selected<=FIXED[kind])
        else:
            require(kind=='user' and type(test_token) is str and re.fullmatch('[0-9a-f]{12}',test_token))
            require(selected<={f'root-alert-stdlib-{test_token}-{n}.service' for n in ('healthy','crash73','dropped','later')})
        self.wire=wire;wire.selected=selected;self.kind=kind;self.selected=selected;self.on_job=on_job;self.now=now
        self.epoch=uuid.uuid4().hex;self.fault=None;self.manager=None;self.client=None;self.bus_id=None
        self.pending=None;self.stream_index=0;self.last_manager_serial=0;self.event_count=0;self.started=False;self.last_clock=None;self.jobs={};self.seen_jobs=set()

    def bad(self,reason):
        if self.fault is None:self.fault=reason
        raise Unknown('collector_unknown')
    def clock(self):
        try:
            t=self.now();require(type(t) is int and t>=0 and (self.last_clock is None or t>=self.last_clock));self.last_clock=t;return t
        except BaseException as error:
            self.fault=self.fault or 'clock_unqualified'
            if not isinstance(error,Exception):raise
            raise Unknown('collector_unknown') from None
    def close(self):
        self.fault=self.fault or 'closed'
        self.wire.close()
    def serial(self,h):
        try:
            n=h['serial']
            if not (type(n) is int and self.last_manager_serial<n<=0xffffffff):self.bad('manager_serial_unqualified')
            self.last_manager_serial=n  # Gaps are NORMAL, not invented message-loss proof.
        except BaseException as error:
            self.fault=self.fault or 'manager_serial_unqualified'
            if not isinstance(error,Exception):raise
            raise Unknown('collector_unknown') from None

    def handle(self,h,body):
        try:
            require(self.fault is None);self.stream_index+=1
            if h['interface']=='org.freedesktop.DBus.Local' and h['member']=='Disconnected':self.bad('disconnected')
            if h['type'] in (2,3):
                require(self.pending is not None and h['reply_serial']==self.pending['serial']
                        and h['sender']==self.pending['sender'])
                if h['sender']==self.manager:self.serial(h)
                if h['type']==3:self.bad('method_error')  # Never read free error text.
                require(h['signature']==self.pending['signature'])
                self.pending['reply']=(body(h['signature']),self.stream_index);return
            if h['type']!=4:return
            if h['interface']==BUS and h['member']=='NameOwnerChanged':
                require(h['sender']==BUS and h['path']==BUS_PATH and h['signature']=='sss')
                name,old,new=body('sss')
                if name==MANAGER:self.bad('manager_owner_changed')
                return
            if h['interface']!=IFACE or h['member'] not in ('JobNew','JobRemoved'):return
            require(self.manager is not None and h['sender']==self.manager and h['path']==MANAGER_PATH)
            self.serial(h);sig='uos' if h['member']=='JobNew' else 'uoss';require(h['signature']==sig)
            values=body(sig);job,path,unit=values[:3]
            require(type(job) is int and 0<job<=0xffffffff and path==f'/org/freedesktop/systemd1/job/{job}')
            if unit not in self.selected:return
            self.event_count+=1;require(self.event_count<=MAX_EVENTS)
            result=values[3] if len(values)==4 else None
            require(result is None or result in ('done','failed','canceled','timeout','dependency','skipped'))
            if h['member']=='JobNew':
                require(job not in self.seen_jobs and len(self.seen_jobs)<MAX_EVENTS)
                self.seen_jobs.add(job);self.jobs[job]=unit
            else:
                require(self.jobs.get(job)==unit)
            require(self.on_job({'member':h['member'],'job_id':job,'unit':unit,'result':result,
                         'stream_index':self.stream_index,'manager_serial':h['serial'],'epoch':self.epoch}) is True)
            if h['member']=='JobRemoved':self.jobs.pop(job)
        except BaseException as error:
            if self.fault is None:self.fault='message_or_callback_unqualified'
            if not isinstance(error,Exception):raise
            raise Unknown('collector_unknown') from None

    def call(self,destination,path,interface,member,signature,arg=None):
        if self.fault is not None:raise Unknown('collector_unknown')
        start=self.clock();deadline=start+BUDGET_NS
        try:
            require(self.pending is None)
            permitted = (destination==BUS and path==BUS_PATH and interface==BUS and (
                member in ('Hello','GetId') and signature=='s' and arg is None
                or member=='GetNameOwner' and signature=='s' and arg==MANAGER
                or member=='AddMatch' and signature=='' and arg in (OWNER_RULE,JOB_RULE))) or (
                destination==MANAGER and path==MANAGER_PATH and interface==IFACE and arg is None
                and (member=='Subscribe' and signature=='' or member=='ListJobs' and signature=='a(usssoo)'))
            require(permitted)
            serial=self.wire.send(destination,path,interface,member,arg)
            require(type(serial) is int and 0<serial<=0xffffffff)
            self.pending={'serial':serial,'sender':BUS if destination==BUS else self.manager,'signature':signature,'reply':None}
            for _ in range(MAX_MESSAGES):
                remaining=deadline-self.clock()
                require(remaining>0)
                self.wire.receive(min(10,max(0,math.floor(remaining/1_000_000))),self.handle)
                if self.pending['reply'] is not None:
                    require(self.clock()<=deadline);return self.pending['reply']
            self.bad('queue_budget')
        except BaseException as error:
            if self.fault is None:self.fault='deadline_or_transport'
            if not isinstance(error,Exception):raise
            raise Unknown('collector_unknown') from None
        finally:self.pending=None

    def start(self):
        try:
            require(not self.started and self.fault is None)
            self.wire.open(self.kind)
            hello,_=self.call(BUS,BUS_PATH,BUS,'Hello','s');require(len(hello)==1 and unique(hello[0]));self.client=hello[0]
            self.wire.peer(self.kind)
            bus,_=self.call(BUS,BUS_PATH,BUS,'GetId','s');require(re.fullmatch('[0-9a-f]{32}',bus[0]));self.bus_id=bus[0]
            self.call(BUS,BUS_PATH,BUS,'AddMatch','',OWNER_RULE)
            owner,_=self.call(BUS,BUS_PATH,BUS,'GetNameOwner','s',MANAGER);require(unique(owner[0]));self.manager=owner[0]
            self.call(BUS,BUS_PATH,BUS,'AddMatch','',JOB_RULE)
            self.call(MANAGER,MANAGER_PATH,IFACE,'Subscribe','')
            rows,index=self.call(MANAGER,MANAGER_PATH,IFACE,'ListJobs','a(usssoo)')
            require(not rows);self.started=True
            return self.receipt(index)
        except BaseException:
            self.fault=self.fault or 'initialization_unqualified';self.wire.close();raise
    def barrier(self):
        try:
            require(self.started and self.fault is None)
            rows,index=self.call(MANAGER,MANAGER_PATH,IFACE,'ListJobs','a(usssoo)')
            require(len({row[0] for row in rows})==len(rows) and {row[0]:row[1] for row in rows}==self.jobs)
            return self.receipt(index)
        except BaseException as error:
            self.fault=self.fault or 'barrier_unqualified'
            if not isinstance(error,Exception):raise
            raise Unknown('collector_unknown') from None
    def receipt(self,index):
        return {'kind':'libdbus_manager_seam_v1','epoch':self.epoch,'bus_id':self.bus_id,'client':self.client,
                'manager':self.manager,'stream_index':index,'fault':self.fault,
                'complete':False,'authority':'MANAGER_METADATA_ONLY'}

if __name__=='__main__':
    print(json.dumps({'kind':'inert_libdbus_collector_plan','complete':False,'live_connection':'UNRUN'}))
