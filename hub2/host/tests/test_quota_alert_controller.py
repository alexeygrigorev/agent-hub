"""Real quota/client/Unix IPC, fake manager and sink; never real mail/provider."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from agent_hub_host import alert_broker as broker_module

ROOT=Path(__file__).resolve().parents[3]
SOURCE=ROOT/'scripts/hub-quota-publish.py'
CLIENT=ROOT/'scripts/hub_alert_client.py'
spec=importlib.util.spec_from_file_location('quota_positive_source',SOURCE)
quota=importlib.util.module_from_spec(spec);spec.loader.exec_module(quota)


class Manager:
    def __init__(self, callback):self.wire=self;self.closed=False
    def start(self):pass
    def receive(self,*args):return False
    def handle(self,*args):raise AssertionError('no manager events fabricated')
    def barrier(self):pass
    def close(self):self.closed=True


class Tests(unittest.TestCase):
    def setUp(self):
        # Short owned repo path also fits AF_UNIX's pathname bound.
        parent=Path.cwd()/'.local/tmp';parent.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='qp-',dir=parent);self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.observer_dir=self.root/'broker';self.observer_dir.mkdir(mode=0o700)
        self.client=self.root/'client.py';self.client.write_bytes(CLIENT.read_bytes());self.client.chmod(0o600)
        self.state=self.root/'quota/state.json';quota.fsync_state_dir(self.state.parent,create=True)
        self.sink=self.root/'sink';self.mail_count=self.root/'mail-count'
        self.sink.write_text('#!/usr/bin/python3\nfrom pathlib import Path\np=Path('+repr(str(self.mail_count))+')\np.write_text(str(int(p.read_text())+1) if p.exists() else "1")\n')
        self.sink.chmod(0o700)
        pins={role:broker_module.ram.SourcePin(1,2,'a'*64) for role in broker_module.ram.ROLES}
        self.broker=broker_module.Broker(self.observer_dir,pins,collector_factory=Manager)
        self.broker.start();self.addCleanup(self.broker.close)
        deadline=time.monotonic()+1
        while self.broker.collector is None and not self.broker.closed and time.monotonic()<deadline:time.sleep(.001)
        self.assertIsNotNone(self.broker.collector)

    def main(self,extra=()):
        args=['--check','--accounts','gemini','--state',str(self.state),'--alert-command',str(self.sink),
            '--alert-client',str(self.client),'--alert-client-sha256',hashlib.sha256(self.client.read_bytes()).hexdigest(),
            '--alert-broker-socket',str(self.broker.path),*extra]
        with patch.dict(os.environ,{},clear=True),contextlib.redirect_stdout(io.StringIO()) as out:
            rc=quota.main(args)
        return rc,json.loads(out.getvalue())

    def seed(self,*,fresh=False,alerted=False):
        quota.write_state(self.state,{'version':1,'accounts':{'gemini':quota.now_ms()-(1 if fresh else 900000)},
                                      'alerted':['gemini'] if alerted else []})

    def test_real_source_decisions_retained_despite_negative_ack_no_count_credit(self):
        self.seed();actual_alert=quota.alert
        def before_mail(*args,**kwargs):
            decisions=[r['frame'] for r in self.broker.unqualified_frames if r['frame']['phase']=='decision']
            self.assertEqual(decisions[-1]['decision'],'alert')
            self.assertEqual(quota.read_state(self.state,quota.now_ms())['alerted'],[])
            return actual_alert(*args,**kwargs)
        with patch.object(quota,'alert',side_effect=before_mail):rc,value=self.main()
        self.assertEqual(rc,2);self.assertTrue(value['alert_sent'])
        rc,value=self.main();self.assertEqual(rc,2);self.assertFalse(value['alert_sent'])
        self.seed(fresh=True,alerted=True);rc,value=self.main();self.assertEqual(rc,0)
        self.assertEqual(self.mail_count.read_text(),'1')
        rows=self.broker.unqualified_frames;decisions=[r['frame'] for r in rows if r['frame']['phase']=='decision']
        self.assertEqual([r['decision'] for r in decisions],['alert','alert_dedup','recovery'])
        self.assertTrue(all(r['observer_ok'] is False for r in decisions),'negative begin ACK stays dirty')
        self.assertTrue(all(r['qualification']=='none' for r in rows));self.assertEqual(len(rows),9)
        contexts={r['frame']['invocation_id'] for r in rows};self.assertEqual(len(contexts),3)
        for context in contexts:
            parts=[r['frame'] for r in rows if r['frame']['invocation_id']==context]
            self.assertEqual([r['phase'] for r in parts],['begin','decision','terminal'])
            self.assertEqual([r['seq'] for r in parts],[0,1,2])
        proof=broker_module.exchange(self.broker.path,{'v':1,'op':'snapshot','nonce':'c'*32})
        self.assertIs(proof['complete'],False);self.assertFalse(proof['ram_consistent'])
        self.assertEqual(self.broker.ram.active,{})
        self.assertTrue(all(v['started']==v['finished']==v['occurrences']==0 for v in self.broker.ram.counts.values()))

    def test_missing_companion_original_alert_runs_once_no_frames(self):
        self.seed();rc,value=self.main(['--alert-client',str(self.root/'missing')])
        self.assertEqual(rc,2);self.assertTrue(value['alert_sent']);self.assertEqual(self.mail_count.read_text(),'1')
        self.assertEqual(self.broker.unqualified_frames,[])

    def test_wrong_companion_hash_or_symlink_is_fail_open_primary(self):
        self.seed();rc,value=self.main(['--alert-client-sha256','a'*64])
        self.assertEqual(rc,2);self.assertEqual(self.mail_count.read_text(),'1')
        self.seed();alias=self.root/'alias';alias.symlink_to(self.client)
        rc,value=self.main(['--alert-client',str(alias)])
        self.assertEqual(rc,2);self.assertEqual(self.mail_count.read_text(),'2');self.assertEqual(self.broker.unqualified_frames,[])

    def test_missing_socket_does_not_retry_source_or_mail(self):
        self.seed();rc,value=self.main(['--alert-broker-socket',str(self.root/'absent.sock')])
        self.assertEqual(rc,2);self.assertTrue(value['alert_sent']);self.assertEqual(self.mail_count.read_text(),'1')
        self.assertEqual(self.broker.unqualified_frames,[])

    def test_stalled_ack_keeps_original_mail_and_state_once(self):
        self.seed();sock=socket.socket(socket.AF_UNIX,socket.SOCK_SEQPACKET)
        path=self.root/'stall.sock';sock.bind(str(path));sock.listen(8)
        try:
            start=time.monotonic();rc,value=self.main(['--alert-broker-socket',str(path)])
            self.assertLess(time.monotonic()-start,2)
        finally:sock.close()
        self.assertEqual(rc,2);self.assertTrue(value['alert_sent']);self.assertEqual(self.mail_count.read_text(),'1')
        self.assertEqual(quota.read_state(self.state,quota.now_ms())['alerted'],['gemini'])

    def test_broker_loss_preserves_partial_begin_and_original_mail(self):
        self.seed();actual_observe=quota.observe
        def lost(*args,**kwargs):
            self.broker.close();return actual_observe(*args,**kwargs)
        with patch.object(quota,'observe',side_effect=lost):rc,value=self.main()
        self.assertEqual(rc,2);self.assertTrue(value['alert_sent']);self.assertEqual(self.mail_count.read_text(),'1')
        self.assertEqual([r['frame']['phase'] for r in self.broker.unqualified_frames],['begin'])
        self.assertTrue(all(self.broker.ram.faults.values()))

    def test_publisher_receipt_and_recovery_use_same_transport_no_context_leak(self):
        self.seed(alerted=True);hub=self.root/'fake-hub';calls=self.root/'hub-calls'
        hub.write_text('#!/usr/bin/python3\nimport os,json\nfrom pathlib import Path\n'
            'assert not any(k.startswith("HUB_ALERT_") for k in os.environ)\n'
            'p=Path('+repr(str(calls))+');p.write_text("1")\n'
            'print(json.dumps({"published":["gemini"],"skipped":{}}))\n');hub.chmod(0o700)
        args=['--accounts','gemini','--state',str(self.state),'--hub',str(hub),
            '--alert-client',str(self.client),'--alert-client-sha256',hashlib.sha256(self.client.read_bytes()).hexdigest(),
            '--alert-broker-socket',str(self.broker.path)]
        with patch.dict(os.environ,{},clear=True),contextlib.redirect_stdout(io.StringIO()) as out:rc=quota.main(args)
        self.assertEqual(rc,0);self.assertEqual(json.loads(out.getvalue())['status'],'published');self.assertEqual(calls.read_text(),'1')
        frames=[r['frame'] for r in self.broker.unqualified_frames]
        self.assertTrue(all(r['role']=='quota_publish' for r in frames))
        self.assertEqual([r['condition'] for r in frames if r['phase']=='decision'],['quota_publication','quota_stale'])
        self.assertEqual([r['decision'] for r in frames if r['phase']=='decision'],['observation','recovery'])
        self.assertEqual(quota.read_state(self.state,quota.now_ms())['alerted'],[])

    def test_source_context_rejects_foreign_role_parent_and_replay(self):
        module=quota.alert_client(self.client,hashlib.sha256(self.client.read_bytes()).hexdigest())
        for env in ({'HUB_ALERT_ROLE':'canary'},{'HUB_ALERT_INVOCATION_ID':'a'*32},
                    {'HUB_ALERT_PARENT_ID':''},{'HUB_ALERT_EVENT_FD':'3'}, {'HUB_ALERT_BROKER_SOCKET':'/foreign'}):
            with self.assertRaises(module.Refused):module.positive_only_context('quota_monitor',socket_path=str(self.broker.path),env=env)
        for role,parent in [('canary',None),('backup_notifier',None),('quota_monitor','a'*32), (True,None)]:
            with self.assertRaises(module.Refused):module.positive_only_context(role,socket_path=str(self.broker.path),parent_id=parent,env={})
        with patch.object(module.uuid,'uuid4',return_value=types.SimpleNamespace(hex='a'*32)):
            child=module.positive_only_context('quota_monitor',socket_path=str(self.broker.path),env={})
            self.assertEqual(child.identity,'a'*32)
            with self.assertRaises(module.Refused):module.positive_only_context('quota_monitor',socket_path=str(self.broker.path),env={})

    def test_budget_expiry_never_connects_or_changes_business(self):
        module=quota.alert_client(self.client,hashlib.sha256(self.client.read_bytes()).hexdigest())
        child=module.positive_only_context('quota_monitor',socket_path=str(self.broker.path),deadline=time.monotonic()-1,env={})
        with patch.object(module.socket,'socket',side_effect=AssertionError('no late connect')):
            self.assertFalse(child.emit('begin'));self.assertTrue(child.dirty)
        self.assertEqual(self.broker.unqualified_frames,[])

    def test_health_threshold_recovery_before_sink_with_unqualified_transport(self):
        # Use real selected client and unchanged health reducer, synthetic health probe.
        args=types.SimpleNamespace(check=True,alert_client=self.client,alert_client_sha256=hashlib.sha256(self.client.read_bytes()).hexdigest(),
            alert_broker_socket=str(self.broker.path),health_state=self.root/'health/state.json',
            alert_command=self.sink,alert_timeout=1)
        quota.fsync_state_dir(args.health_state.parent,create=True)
        with patch.dict(os.environ,{},clear=True):observer=quota.AlertObserver(args,'quota_monitor')
        observer.emit('begin')
        with patch.object(quota,'sample_health',return_value={'healthy':False,'stage':'public','reason':'not_ok'}),\
            patch.object(quota,'now_ms',side_effect=[1000,2000,3000]):
            for _ in range(3):quota.check_health(args,observer)
        with patch.object(quota,'sample_health',return_value={'healthy':True,'stage':'public','reason':'ok'}),\
            patch.object(quota,'now_ms',return_value=4000):quota.check_health(args,observer)
        observer.emit('terminal')
        rows=[r['frame']['decision'] for r in self.broker.unqualified_frames if r['frame']['phase']=='decision']
        self.assertEqual(rows,['observation','alert','alert_dedup','recovery'])
        self.assertEqual(self.mail_count.read_text(),'1');self.assertFalse(quota.private_json(args.health_state)['alerted'])

    def test_health_state_error_records_fixed_error_without_inventing_delivery(self):
        state=self.root/'health/state.json';quota.fsync_state_dir(state.parent,create=True);state.write_text('not JSON');state.chmod(0o600)
        args=types.SimpleNamespace(check=True,alert_client=self.client,alert_client_sha256=hashlib.sha256(self.client.read_bytes()).hexdigest(),
            alert_broker_socket=str(self.broker.path),health_state=state)
        with patch.dict(os.environ,{},clear=True):observer=quota.AlertObserver(args,'quota_monitor')
        observer.emit('begin')
        with patch.object(quota,'sample_health',return_value={'healthy':False,'stage':'public','reason':'not_ok'}):value=quota.check_health(args,observer)
        observer.emit('terminal');self.assertEqual(value['status'],'health_unknown');self.assertFalse(value['alert_sent'])
        decisions=[r['frame'] for r in self.broker.unqualified_frames if r['frame']['phase']=='decision']
        self.assertEqual([r['condition'] for r in decisions],['error']);self.assertTrue(all(r['observer_ok'] is False for r in decisions))


if __name__=='__main__':unittest.main(verbosity=2)
