"""Pure boundary tests: network/process launches forbidden; public scenario UNRUN."""
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch

import native_mail_public_fixture as public
from native_mail_restart_fixture import make_fixture_bridge, FixtureViolation
from agent_hub_host.client import Client


class PublicNativeMailFixtureGuards(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(prefix='mp-',dir=os.environ.get('RUNNER_TEMP') or None)
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name); self.root.chmod(0o700)
        self.channel=Mock(); self.budget=public.Budget(1000)
        self.value={'origin':'http://127.0.0.1:49152','token':'synthetic-test-token',
            'directory':str(self.root),'mode':'crash','machine_id':'fixture-machine',
            'binding':{'session_id':'fixture-session','native_id':'61ee2e26-87f1-4753-9a0e-83f7ead5e5c5',
                'operation_id':'fixture-operation','epoch':1,'workspace':str(self.root),'tag':'fixture-tag'},
            'recipient':'agent:owned-forward','first_body':'first synthetic body',
            'later_body':'later synthetic body','remaining_ms':1000}
        for target in ('socket.create_connection','socket.socket','subprocess.Popen','subprocess.run',
                       'urllib.request.build_opener'):
            guard=patch(target,side_effect=AssertionError('forbidden real I/O'))
            guard.start(); self.addCleanup(guard.stop)
    def message(self,which='first',identity='message-1'):
        return {'id':identity,'epoch':1,'session_id':self.value['binding']['session_id'],
            'recipient':self.value['recipient'],'operation_id':'delivery-'+identity,
            'body':self.value[which+'_body']}
    def scope(self): return public.Scope(self.value,self.root,self.channel)
    def path(self,row):
        from urllib.parse import quote,urlencode
        return '/api/messages/'+quote(row['id'],safe='')+'/delivered?'+urlencode({'mailbox':row['recipient']})

    def test_exact_schema_origin_and_scalar_guards_before_io(self):
        self.assertIs(public.config(self.value),self.value)
        variants=[{'origin':origin} for origin in ('https://127.0.0.1:1','http://localhost:1',
            'http://127.0.0.1:1/','http://127.0.0.1:1?x','http://127.0.0.1:65536',
            'http://127.0.0.1.evil:1','http://user@127.0.0.1:1')]
        variants += [{'remaining_ms':value} for value in (True,0,35001,1.5,float('nan'))]
        variants += [{'token':'bad\nsecret'},{'first_body':self.value['later_body']},
                     {'messages':[]},{'mode':'provider'}]
        for change in variants:
            with self.subTest(change=change.keys()):
                value=copy.deepcopy(self.value); value.update(change)
                with self.assertRaises(FixtureViolation): public.config(value)
        for raw in ('{"a":1,"a":2}','{"a":NaN}'):
            with self.assertRaises(FixtureViolation): public.decode(raw)

    def test_scope_learns_only_actual_valid_frames_and_is_immutable(self):
        scope=self.scope(); first=self.message(); scope.receive(first)
        self.assertEqual(scope.rows,{'first':first})
        scope.receive(first)  # genuine replay preserves binding; it does not ACK.
        for field,value in (('session_id','foreign'),('recipient','foreign'),('body','foreign'),
                            ('id','foreign-id'),('epoch',True),('operation_id','foreign-op')):
            row=dict(first); row[field]=value
            with self.subTest(field=field),self.assertRaises(FixtureViolation): scope.receive(row)
        with self.assertRaises(FixtureViolation): scope.effect(first['id'],'foreign')
        with self.assertRaises(FixtureViolation): scope.receive(self.message('later','message-2'))
        self.assertEqual(public.read_json(self.root/'first.observed.json'),first)

    def test_restart_same_observation_allows_only_later_effect_and_never_first_receipt(self):
        scope=self.scope(); first=self.message(); scope.receive(first)
        self.value['mode']='receive'; scope=self.scope()
        later=self.message('later','message-2'); scope.receive(later)
        scope.effect(later['id'],later['body'])
        with self.assertRaises(FixtureViolation): scope.effect(first['id'],first['body'])
        claim={'phase':'claim','epoch':1,'operation_id':first['operation_id']}
        self.assertEqual(scope.api('POST',self.path(first),claim,None)[0],'first')
        receipt={'phase':'receipt','epoch':1,'operation_id':first['operation_id'],
                 'claim_id':'claim','native_receipt':{}}
        with self.assertRaises(FixtureViolation): scope.api('POST',self.path(first),receipt,None)
        for method,path in (('GET',self.path(first)),('POST','/api/heartbeat'),('POST','https://foreign/api/x')):
            with self.assertRaises(FixtureViolation): scope.api(method,path,claim,None)
        self.assertEqual(public.read_json(self.root/'first.observed.json'),first)

    def test_real_client_methods_called_once_return_unmodified_and_no_first_ack(self):
        scope=self.scope(); first=self.message(); scope.receive(first)
        client=public.PublicClient(self.value,self.budget,self.channel,scope)
        sentinel={'claim_id':'actual-response','epoch':1}
        with patch.object(Client,'request',return_value=sentinel) as request:
            result=client.request('POST',self.path(first),{'phase':'claim','epoch':1,
                                  'operation_id':first['operation_id']})
            self.assertIs(result,sentinel); self.assertEqual(request.call_count,1)
        raw=MagicMock()
        with patch.object(Client,'socket',return_value=raw) as create:
            wrapper=client.socket('fixture-machine')
            self.assertIs(wrapper.raw,raw); self.assertEqual(create.call_count,1)
        with patch.object(Client,'request') as request:
            with self.assertRaises(FixtureViolation):
                client.request('POST',self.path(first),{'phase':'receipt','epoch':1,
                    'operation_id':first['operation_id'],'claim_id':'x','native_receipt':{}})
            request.assert_not_called()

    def test_ws_validates_before_real_dispatch_and_observes_only_after_real_send(self):
        scope=self.scope(); row=self.message(); client=public.PublicClient(self.value,self.budget,self.channel,scope)
        raw=MagicMock(); wrapper=public.ObservedSocket(raw,client)
        raw.recv.return_value=json.dumps({'type':'mailbox.deliver','message':row})
        self.assertEqual(public.decode(wrapper.recv())['message'],row)
        self.assertIn('first',scope.rows)
        for event in ({'type':'session.offer','offer':{}},{'type':'session.command','command':{}},
                      {'type':'engine.update','job':{}},{'type':'resync','reason':'foreign'},
                      {'type':'mailbox.deliver','message':{**row,'session_id':'foreign'}}):
            raw.recv.return_value=json.dumps(event)
            with self.assertRaises(FixtureViolation): wrapper.recv()
        frame={'type':'host.error','reason':'native_mail_outcome_uncertain',
               'session_id':row['session_id'],'message_id':row['id'],'operation_id':row['operation_id']}
        raw.send.side_effect=RuntimeError('synthetic-send-failure')
        self.channel.reset_mock()
        with self.assertRaises(RuntimeError): wrapper.send(json.dumps(frame))
        self.channel.observe.assert_not_called()
        raw.send.side_effect=None; wrapper.send(json.dumps(frame))
        self.channel.observe.assert_called_once_with('sent_error',message_id=row['id'],reason='native_mail_outcome_uncertain')

    def test_fixed_native_guard_rejects_all_other_execution(self):
        bridge=make_fixture_bridge(self.root,Mock(),'receive',self.value['binding'])
        self.addCleanup(bridge.journal.close_thread)
        for args in ([os.defpath,'start'],['a','status','foreign','--json'],
                     ['a','message','send'],['a','stop','anything']):
            with self.subTest(args=args),self.assertRaises(FixtureViolation): bridge.fixture_native(args)
        status=bridge.fixture_native([public.sys.executable,'status',self.value['binding']['native_id'],'--json'])
        row=json.loads(status.stdout)
        self.assertEqual(row['id'],self.value['binding']['native_id']); self.assertEqual(row['phase'],'running')

    def test_no_delivery_input_and_no_launch_work(self):
        channel=object.__new__(public.Channel); channel.line=Mock(return_value={'type':'mailbox.deliver','message':self.message()})
        with self.assertRaises(FixtureViolation): channel.control()
        channel.line.return_value={'control':'stop'}
        with self.assertRaises(public.StopServe): channel.control()
        channel.line.return_value={'control':'disconnect'}
        with self.assertRaises(ConnectionResetError): channel.control()
        with self.assertRaises(FixtureViolation): public.NoWorkPool().submit('provider-launch')

    def test_expired_budget_and_enter_observation_failure_close_owned_socket(self):
        self.budget.end=0
        with self.assertRaises(FixtureViolation): self.budget.left()
        raw=MagicMock(); owner=Mock(); owner.channel.observe.side_effect=FixtureViolation('synthetic')
        owner.telemetry=public.RetryTelemetry(Mock())
        with self.assertRaises(FixtureViolation): public.ObservedSocket(raw,owner).__enter__()
        raw.__exit__.assert_called_once()




class RetryTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.budget=Mock(); self.telemetry=public.RetryTelemetry(self.budget)
        self.telemetry.connecting(); self.output=[]
        writer=patch.object(public.os,'write',side_effect=self.write)
        ready=patch.object(public.select,'select',return_value=([],[2],[]))
        writer.start(); ready.start(); self.addCleanup(writer.stop); self.addCleanup(ready.stop)
    def write(self,fd,raw):
        self.assertEqual(fd,2); self.output.append(raw); return len(raw)
    def retry(self,error,stage='receive'):
        try:
            with self.telemetry.at(stage): raise error
        except Exception as caught:
            self.telemetry.retry(); return caught
    def test_ordinary_failure_is_same_exception_fixed_metadata_only(self):
        error=RuntimeError('secret token URL traceback raw frame')
        self.assertIs(self.retry(error),error)
        self.assertEqual(json.loads(self.output[0]),{'kind':'retry_failure',
            'connection_generation':1,'stage':'receive','exception_class':'RUNTIME'})
        self.assertNotIn(b'secret',self.output[0])
    def test_normal_poll_timeout_has_no_record_without_disconnect(self):
        error=TimeoutError('private')
        with self.assertRaises(TimeoutError) as caught:
            with self.telemetry.at('receive'): raise error
        self.assertIs(caught.exception,error)
        self.assertIsNone(self.telemetry.failure); self.assertEqual(self.output,[])
        owner=Mock(); owner.telemetry=self.telemetry; owner.budget=self.budget
        raw=Mock(); raw.recv.side_effect=error
        with self.assertRaises(TimeoutError) as wrapped:
            public.ObservedSocket(raw,owner).recv(timeout=.1)
        self.assertIs(wrapped.exception,error); self.assertEqual(self.output,[])
    def test_controls_and_fatal_guards_propagate_without_retry_record(self):
        for error in (KeyboardInterrupt(),public.StopServe(),public.FixtureViolation('private')):
            with self.assertRaises(type(error)) as caught:
                with self.telemetry.at('receive'): raise error
            self.assertIs(caught.exception,error)
        self.assertEqual(self.output,[])
    def test_unknown_class_never_serializes_arbitrary_class_or_message(self):
        class SecretNamedError(Exception): pass
        self.retry(SecretNamedError('sensitive'), 'send')
        self.assertEqual(json.loads(self.output[0])['exception_class'],'OTHER')
        self.assertNotIn(b'Secret',self.output[0])
    def test_connection_generation_monotonic_and_failure_not_reused(self):
        self.retry(ValueError(), 'claim_request'); self.telemetry.connecting()
        try: raise RuntimeError()
        except RuntimeError: self.telemetry.retry()
        self.assertEqual([json.loads(r)['connection_generation'] for r in self.output],[1,2])
        self.assertEqual(json.loads(self.output[-1])['stage'],'bridge_unknown')
    def test_record_cap_is_sticky_unknown_and_no_progress(self):
        for _ in range(40): self.retry(RuntimeError())
        self.assertEqual(len(self.output),16); self.assertTrue(self.telemetry.unknown)
        self.assertLessEqual(sum(map(len,self.output)),4096)
        self.telemetry.connecting(); self.retry(RuntimeError()); self.assertEqual(len(self.output),16)
        self.telemetry=public.RetryTelemetry(self.budget); self.telemetry.bytes=4090
        self.retry(RuntimeError()); self.assertTrue(self.telemetry.unknown)
        self.assertEqual(len(self.output),16)
    def test_drop_and_short_write_do_not_retry_or_heal(self):
        with patch.object(public.select,'select',return_value=([],[],[])):
            self.retry(RuntimeError())
        self.assertTrue(self.telemetry.unknown); self.assertEqual(self.output,[])
        self.retry(RuntimeError()); self.assertEqual(self.output,[])
        self.telemetry=public.RetryTelemetry(self.budget)
        with patch.object(public.os,'write',return_value=1) as writer:
            self.retry(RuntimeError()); self.assertEqual(writer.call_count,1)
        self.assertTrue(self.telemetry.unknown)
    def test_expired_budget_or_write_error_is_unknown_not_retry(self):
        self.budget.left.side_effect=ValueError('private')
        self.retry(RuntimeError()); self.assertTrue(self.telemetry.unknown)
        self.assertEqual(self.output,[])
        self.budget.left.side_effect=None; self.telemetry=public.RetryTelemetry(self.budget)
        with patch.object(public.os,'write',side_effect=BlockingIOError('private')):
            self.retry(RuntimeError())
        self.assertTrue(self.telemetry.unknown)
    def test_exact_disconnect_logger_boundary_only(self):
        original=Mock(); proxy=public.RetryLogging(original,self.telemetry)
        proxy.exception('other private payload',object())
        self.assertEqual(self.output,[])
        try: raise TypeError('secret')
        except TypeError:
            proxy.getLogger('agent_hub_host.agent').exception('Host disconnected machine=%s','private machine')
        self.assertEqual(json.loads(self.output[0])['exception_class'],'TYPE')
        self.assertNotIn(b'private',self.output[0])
        self.assertIs(proxy.getLogger('other'),original.getLogger.return_value)


if __name__=='__main__': unittest.main()
