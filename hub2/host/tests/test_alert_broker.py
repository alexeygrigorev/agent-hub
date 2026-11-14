"""Private candidate tests: real Unix IPC, fake manager; no libdbus activation."""
import ast
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
from agent_hub_host import alert_broker as b
r = b.ram
BOOT = 'a'*8+'-'+ 'a'*4+'-'+ 'a'*4+'-'+ 'a'*4+'-'+ 'a'*12
KEY = r.ProcessKey(123, 456, BOOT)
PINS = {role:r.SourcePin(1,2,'a'*64) for role in r.ROLES}


def frame():
    return dict(v=1,invocation_id='a'*32,parent_id=None,role='quota_monitor',phase='begin',
        seq=0,decision=None,condition=None,flags={},recipient_class='none',observer_ok=True)


class Manager:
    def __init__(self, callback):
        self.callback=callback;self.closed=False;self.calls=[];self.wire=self
        self.fail=False
    def start(self):self.calls.append('start')
    def receive(self, *_):
        if self.fail:raise OSError('synthetic loss')
        return False
    def handle(self, *_):raise AssertionError('no fake messages')
    def barrier(self):self.calls.append('barrier')
    def close(self):self.closed=True


class Tests(unittest.TestCase):
    def broker(self, directory):
        return b.Broker(directory,PINS,collector_factory=Manager,identity=lambda pid:KEY)

    def temporary(self):
        return tempfile.TemporaryDirectory(prefix='resident-')

    def test_approved_core_bytes_exact(self):
        for name,digest in [('alert_authority.py','e11f786d91efc902716384b4d00e03a9cbb8615ac955a717ac96715fb16c9d18'),
            ('alert_collector.py','1db01dae6b1ef4187281e986a50d00521cd1d2942fef4e8e9e486b242ab84fb8')]:
            self.assertEqual(hashlib.sha256((Path(b.__file__).parent/name).read_bytes()).hexdigest(),digest)

    def test_disabled_start_has_no_io(self):
        with patch.object(b,'Broker',side_effect=AssertionError('I/O forbidden')):
            self.assertIsNone(b.start_optional())
            self.assertIsNone(b.start_optional({'wrong':1}))
        with patch.object(b.sys,'platform','win32'),patch.object(b,'Broker',side_effect=AssertionError('Unix forbidden')):
            self.assertIsNone(b.start_optional({'directory':'unused','source_pins':{}}))

    def test_configured_validates_without_activation_or_mutating_config(self):
        value={'v':1,'enabled':True,'directory':'/run/user/1000/owned-observer',
            'source_pins':{role:{'dev':1,'ino':2,'sha256':'a'*64} for role in r.ROLES}}
        before=json.dumps(value,sort_keys=True)
        with patch.object(b,'Broker',side_effect=AssertionError('activation forbidden')):
            self.assertEqual(b.configured(value)['directory'],value['directory'])
            for change in ({'v':True},{'enabled':False},{'directory':'/x/../y'},
                {'directory':'relative'},{'directory':'/'+'x'*81},{'source_pins':{}},{'secret':'not accepted'}):
                altered=dict(value);altered.update(change);self.assertIsNone(b.configured(altered))
        self.assertEqual(json.dumps(value,sort_keys=True),before)

    def test_hooks_cannot_self_admit_or_create_zero_authority(self):
        v=self.broker('/unused')
        answer=v.request({'v':1,'op':'hook','frame':frame()},(123,os.getuid(),os.getgid()))
        self.assertFalse(answer['ok']);self.assertEqual(v.ram.active,{})
        self.assertTrue(all(n['started']==0 for n in v.ram.counts.values()))
        snap=v.request({'v':1,'op':'snapshot','nonce':'b'*32},(123,os.getuid(),os.getgid()))
        self.assertIs(snap['complete'],False);self.assertFalse(snap['ram_consistent'])

    def test_unknown_admit_and_source_open_requests_refuse(self):
        for op in ('admit','source_open','dispatch','spawn'):
            v=self.broker('/unused')
            with self.assertRaises(b.Refused):v.request({'v':1,'op':op},(123,os.getuid(),0))
            self.assertTrue(all(v.ram.faults.values()))

    def test_changed_peer_birth_and_wrong_uid_refuse(self):
        v=self.broker('/unused');v.identity=lambda pid:r.ProcessKey(pid,456,BOOT)
        with self.assertRaises(b.Refused):v.request({'v':1,'op':'snapshot','nonce':'b'*32},(123,os.getuid()+1,0))
        v=self.broker('/unused');seq=iter([KEY,r.ProcessKey(123,457,BOOT)]);v.identity=lambda pid:next(seq)
        with self.assertRaises(b.Refused):v.request({'v':1,'op':'snapshot','nonce':'b'*32},(123,os.getuid(),0))

    def test_duplicate_json_and_truncation_refuse(self):
        for raw in (b'{"v":1,"v":1}',b'{',b'NaN',b' '*2049):
            with self.assertRaises(Exception):b.decode(raw,b.FRAME)

    def test_metadata_never_admits_capacity_is_sticky(self):
        v=self.broker('/unused')
        for n in range(b.MAX_JOBS):self.assertTrue(v.job({'unit':'selected','job_id':n}))
        self.assertEqual(v.ram.active,{})
        with self.assertRaises(b.Refused):v.job({'job_id':999})
        self.assertTrue(v.ram.faults['quota_stale']);self.assertEqual(len(v.events),b.MAX_JOBS)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux user-manager IPC')
    def test_actual_unix_roundtrip_fake_manager_and_cleanup(self):
        with self.temporary() as directory:
            v=self.broker(directory);v.start()
            end=time.monotonic()+1
            while v.collector is None and not v.closed and time.monotonic()<end:time.sleep(.001)
            answer=b.exchange(v.path,{'v':1,'op':'snapshot','nonce':'c'*32})
            self.assertIs(answer['complete'],False);self.assertIn('barrier',v.collector.calls)
            child=b.Client(v.path,'a'*32,None,'quota_monitor')
            self.assertFalse(child.emit('begin'));self.assertTrue(child.dirty)
            v.close();self.assertTrue(v.closed);self.assertTrue(v.collector.closed)
            self.assertFalse(v.path.exists())

    @unittest.skipUnless(sys.platform == 'linux', 'Linux user-manager IPC')
    def test_foreign_socket_never_unlinked(self):
        with self.temporary() as directory:
            sock=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET)
            path=Path(directory)/'observer.sock';sock.bind(str(path));inode=path.stat().st_ino
            try:
                v=self.broker(directory);v.start();v.thread.join(1)
                self.assertTrue(v.closed);self.assertEqual(path.stat().st_ino,inode)
            finally:sock.close()

    @unittest.skipUnless(sys.platform == 'linux', 'Linux user-manager IPC')
    def test_manager_loss_no_replacement_or_healing(self):
        with self.temporary() as directory:
            v=self.broker(directory);v.start()
            end=time.monotonic()+1
            while v.collector is None and time.monotonic()<end:time.sleep(.001)
            first=v.collector;first.fail=True;v.thread.join(1)
            self.assertTrue(v.closed);self.assertIs(v.collector,first)
            self.assertTrue(all(v.ram.faults.values()));v.close()

    @unittest.skipUnless(sys.platform == 'linux', 'Linux user-manager IPC')
    def test_actual_socket_deadline_no_ack_and_no_replay(self):
        with self.temporary() as directory:
            path=str(Path(directory)/'silent');sock=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET)
            sock.bind(path);sock.listen(1);start=time.monotonic()
            try:
                with self.assertRaises(b.Refused):b.exchange(path,{'v':1})
                elapsed=time.monotonic()-start
                # Scheduling slack for the test, production deadline remains150ms.
                self.assertLess(elapsed,.30);self.assertGreater(elapsed,.12)
                sock.setblocking(False);accepted,_=sock.accept();accepted.close()
                with self.assertRaises(BlockingIOError):sock.accept()
            finally:sock.close()

    def test_expired_absolute_budget_never_gets_floor(self):
        with self.assertRaises(b.Refused):b.left(time.monotonic()-1)

    def test_client_dirty_after_bad_ack_preserves_business_return(self):
        effect=[]
        c=b.Client('/unused','a'*32,None,'canary')
        with patch.object(b,'exchange',return_value={'seq':0,'ok':False}) as send:
            self.assertFalse(c.emit('begin'));effect.append('original_once')
            self.assertTrue(c.dirty);self.assertEqual(send.call_count,1)
        self.assertEqual(effect,['original_once'])

    def test_client_absolute_budget_shared_with_exchange(self):
        c=b.Client('/unused','a'*32,None,'canary')
        with patch.object(b.time,'monotonic',return_value=20),patch.object(b,'exchange',return_value={'seq':0,'ok':True}) as send:
            self.assertTrue(c.emit('begin'))
        self.assertEqual(send.call_args.kwargs['deadline'],20.150)

    def implementations(self):
        spec=importlib.util.spec_from_file_location('standalone_v2',ROOT/'scripts/hub_alert_client.py')
        mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
        return (b,mod)

    def test_both_clients_reject_exact_illtyped_ack_probes(self):
        for mod in self.implementations():
            for seq,answer in [(0,{'seq':False,'ok':1}),(0,{'seq':0.0,'ok':True}),
                (1,{'seq':True,'ok':True}),(0,{'seq':0,'ok':1}),
                (0,{'seq':0,'ok':True,'extra':0})]:
                client=mod.Client('/unused','a'*32,None,'canary');client.seq=seq
                with self.subTest(implementation=mod.__name__,answer=answer),patch.object(mod,'exchange',return_value=answer):
                    self.assertFalse(client.emit('begin'));self.assertTrue(client.dirty)
            client=mod.Client('/unused','a'*32,None,'canary')
            with patch.object(mod,'exchange',return_value={'seq':0,'ok':True}):
                self.assertTrue(client.emit('begin'));self.assertFalse(client.dirty)

    def test_both_clients_latch_interrupt_before_reraise_and_never_heal(self):
        for mod in self.implementations():
            for interrupt in (KeyboardInterrupt(),SystemExit(7),GeneratorExit()):
                client=mod.Client('/unused','a'*32,None,'canary')
                with patch.object(mod,'exchange',side_effect=interrupt) as send:
                    with self.assertRaises(type(interrupt)) as caught:client.emit('begin')
                    self.assertIs(caught.exception,interrupt);self.assertTrue(client.dirty);self.assertEqual(send.call_count,1)
                with patch.object(mod,'exchange',return_value={'seq':1,'ok':True}) as send:
                    self.assertFalse(client.emit('terminal'));self.assertTrue(client.dirty)
                    self.assertIs(send.call_args.args[1]['frame']['observer_ok'],False)

    def test_both_expired_deadlines_have_zero_socket_effects(self):
        for mod in self.implementations():
            with patch.object(mod.socket,'socket',side_effect=AssertionError('socket forbidden')) as create:
                with self.assertRaises(mod.Refused):mod.exchange('/unused',{'v':1},deadline=time.monotonic()-1)
                create.assert_not_called()

    def test_both_clients_latch_every_entry_boundary_before_exchange(self):
        for mod in self.implementations():
            for interrupt in (KeyboardInterrupt(),SystemExit(7),GeneratorExit()):
                for boundary in ('clock','flags','sequence'):
                    client=mod.Client('/unused','a'*32,None,'canary')
                    class Broken:
                        def __bool__(self):raise interrupt
                        def __add__(self, other):raise interrupt
                    kwargs={}
                    if boundary=='flags':kwargs['flags']=Broken()
                    if boundary=='sequence':client.seq=Broken()
                    with patch.object(mod,'exchange') as exchange,patch.object(mod.time,'monotonic',
                        side_effect=interrupt if boundary=='clock' else None,return_value=20):
                        with self.assertRaises(type(interrupt)) as caught:client.emit('begin',**kwargs)
                        self.assertIs(caught.exception,interrupt);self.assertTrue(client.dirty)
                        exchange.assert_not_called()

    def test_owned_resources_each_close_once_after_independent_close_errors(self):
        from unittest.mock import MagicMock
        for node_acquired in (False,True):
            v=self.broker('/unused');sock=MagicMock();sock.close.side_effect=OSError('close uncertain')
            owned=types.SimpleNamespace(st_mode=stat.S_IFSOCK|0o777,st_uid=os.getuid(),st_nlink=1,st_dev=9,st_ino=10)
            if not node_acquired:sock.bind.side_effect=OSError('bind refused')
            def close(fd):
                if fd==81:raise OSError('node close uncertain')
            with patch.object(b,'private_directory',return_value=80),patch.object(b.socket,'socket',return_value=sock),\
                patch.object(b.os,'open',return_value=81),patch.object(b.os,'fstat',return_value=owned),\
                patch.object(b.os,'stat',return_value=owned),patch.object(b.os,'chmod',side_effect=OSError('chmod failed')),\
                patch.object(b.os,'unlink'),patch.object(b.os,'close',side_effect=close) as closes:
                v._run()
            self.assertEqual(sock.close.call_count,1)
            self.assertEqual([call.args[0] for call in closes.call_args_list],[81,80] if node_acquired else [80])
            self.assertIsNone(v.node_fd);self.assertIsNone(v.directory_fd);self.assertIsNone(v.listener)
            self.assertTrue(v.closed);self.assertTrue(all(v.ram.faults.values()))

    def test_both_client_asts_match_canonical_standalone_companion(self):
        tree=ast.parse((ROOT/'scripts/hub_alert_client.py').read_text());host=ast.parse(Path(b.__file__).read_text())
        for name in ('Client','exchange','checked','decode','wait','left'):
            def node(t):return next(n for n in t.body if isinstance(n,(ast.FunctionDef,ast.ClassDef)) and n.name==name)
            self.assertEqual(ast.dump(node(tree)),ast.dump(node(host)))

    def test_foreign_postbind_node_is_not_chmodded_listened_or_unlinked(self):
        for refused in (False,True):
            from unittest.mock import MagicMock
            v=self.broker('/unused');sock=MagicMock()
            foreign=types.SimpleNamespace(st_mode=stat.S_IFSOCK|0o777,st_uid=os.getuid()+1,st_nlink=1,st_dev=9,st_ino=10)
            with patch.object(b,'private_directory',return_value=80),patch.object(b.socket,'socket',return_value=sock),\
                patch.object(b.os,'open',return_value=81),patch.object(b.os,'fstat',return_value=foreign),\
                patch.object(b.os,'close'),patch.object(b.os,'chmod',side_effect=PermissionError('foreign') if refused else None) as chmod,patch.object(b.os,'unlink') as unlink:
                v._run()
            sock.bind.assert_called_once_with('/proc/self/fd/80/observer.sock')
            chmod.assert_not_called();unlink.assert_not_called();sock.listen.assert_not_called()
            self.assertIsNone(v.socket_identity);self.assertTrue(v.closed)

    @unittest.skipUnless(sys.platform == 'linux', 'Linux user-manager IPC')
    def test_directory_path_swap_never_redirects_anchored_cleanup(self):
        with self.temporary() as outer:
            directory=Path(outer)/'owned';directory.mkdir(mode=0o700)
            v=self.broker(directory);v.start();end=time.monotonic()+1
            while v.collector is None and not v.closed and time.monotonic()<end:time.sleep(.001)
            self.assertIsNotNone(v.collector)
            moved=Path(outer)/'moved';directory.rename(moved);directory.mkdir(mode=0o700)
            foreign=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET)
            try:
                foreign.bind(str(directory/'observer.sock'));inode=(directory/'observer.sock').stat().st_ino
                v.close();self.assertTrue(v.closed)
                self.assertFalse((moved/'observer.sock').exists())
                self.assertEqual((directory/'observer.sock').stat().st_ino,inode)
            finally:foreign.close()


    def test_same_real_serve_runs_ws_when_optional_factory_fails(self):
        tree=ast.parse((Path(b.__file__).parent/'agent.py').read_text())
        bridge=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Bridge')
        serve=next(n for n in bridge.body if isinstance(n,ast.FunctionDef) and n.name=='serve')
        class Thread:
            def __init__(self,**kw):pass
            def start(self):pass
            def join(self,**kw):pass
        class Pool:
            def __init__(self,**kw):pass
            def shutdown(self,**kw):pass
        calls=[]
        ns={'__package__':'agent_hub_host','logging':types.SimpleNamespace(basicConfig=lambda **kw:None,INFO=1),
            '_NATIVE_INVENTORY_START_LOCK':threading.Lock(),
            'credential_delivery':types.SimpleNamespace(start=lambda x:None,stop=lambda x:calls.append('credential_stop')),
            'ThreadPoolExecutor':Pool,'threading':types.SimpleNamespace(Thread=Thread,Event=threading.Event,BoundedSemaphore=threading.BoundedSemaphore)}
        exec(compile(ast.Module(body=[serve],type_ignores=[]),'<actual-serve>','exec'),ns)
        host=types.SimpleNamespace(_alert_observer_spec={'opt':'in'},_serve=lambda *args:calls.append('ws'))
        with patch.object(b,'start_optional',side_effect=OSError('optional failure')):
            ns['serve'](host,'machine',[])
        self.assertEqual(calls,['ws','credential_stop'])

    @unittest.skipUnless(sys.platform == 'linux', 'Linux user-manager IPC')
    def test_close_never_waits_for_stuck_manager_call(self):
        release=threading.Event();entered=threading.Event()
        class Stuck(Manager):
            def start(self):entered.set();release.wait(2)
        with self.temporary() as directory:
            v=self.broker(directory);v.factory=Stuck;v.start();self.assertTrue(entered.wait(1))
            start=time.monotonic();v.close();self.assertLess(time.monotonic()-start,.35)
            release.set();v.thread.join(1);self.assertTrue(v.closed)
            self.assertTrue(all(v.ram.faults.values()))

    def test_lifecycle_is_outside_ws_reconnection(self):
        tree=ast.parse((Path(b.__file__).parent/'agent.py').read_text())
        bridge=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Bridge')
        serve=next(n for n in bridge.body if isinstance(n,ast.FunctionDef) and n.name=='serve')
        reconnect=next(n for n in bridge.body if isinstance(n,ast.FunctionDef) and n.name=='_serve')
        self.assertIn('start_optional',ast.unparse(serve));self.assertIn('stop_optional',ast.unparse(serve))
        self.assertNotIn('alert_broker',ast.unparse(reconnect))


if __name__=='__main__':unittest.main()
