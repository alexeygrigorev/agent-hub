"""A stop confirms exact native death even when retirement trails kill."""
import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from agent_hub_host.agent import Bridge, Journal

NATIVE='11111111-2222-4333-8444-555555555555'

class StopRetirementWaitTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.journal=Journal(self.root/'journal.sqlite')
        self.addCleanup(self.journal.db.close)
        with self.journal.db:
            self.journal.db.execute("INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,interactive) VALUES(?,?,?,?,?,?,?,'running',1)",('op','session',1,'digest',str(self.root),'task-session',NATIVE))
        self.bridge=Bridge.__new__(Bridge)
        self.bridge.journal=self.journal
        self.bridge.launcher=types.SimpleNamespace(state_dir=self.root)
        self.live={'id':NATIVE,'tag':'task-session','workspace':str(self.root),'phase':'running','created_at_ms':1000}
        self.dead={**self.live,'phase':'exited','containment_empty':True,'exit':{'code':0,'exited_at_ms':2000}}
        self.bridge.resolve_launch_native=lambda row:(NATIVE,self.live)
        self.bridge.native=Mock()
        self.clock=0
        def sleep(seconds):self.clock+=seconds
        self.patches=[patch('agent_hub_host.agent.time.monotonic',side_effect=lambda:self.clock),patch('agent_hub_host.agent.time.time',side_effect=lambda:100+self.clock),patch('agent_hub_host.agent.time.sleep',side_effect=sleep)]
        for p in self.patches:p.start();self.addCleanup(p.stop)

    def stop(self,deadline=102000):
        return self.bridge.command({'id':'stop-command','session_id':'session','epoch':1,'kind':'stop','deadline_ms':deadline})

    def test_delayed_qualified_retirement_is_confirmed_and_replayed_without_kill(self):
        self.bridge.lookup=Mock(side_effect=[self.live,None,self.dead])
        result=self.stop();self.assertTrue(result['stopped']);self.assertEqual(result['native_id'],NATIVE)
        self.assertEqual(self.bridge.lookup.call_count,3)
        self.assertEqual(self.stop(),result);self.bridge.native.assert_called_once_with('kill',NATIVE)
        ack=json.loads(self.journal.db.execute('SELECT payload FROM outbox').fetchone()[0]);self.assertEqual(ack['result'],result)

    def test_no_evidence_is_never_success_and_wait_stays_within_deadline(self):
        self.bridge.lookup=Mock(return_value=None)
        result=self.stop(100250);self.assertFalse(result['stopped']);self.assertLessEqual(self.clock,.251)
        self.assertGreater(self.bridge.lookup.call_count,1)

    def test_qualified_retirement_checks_original_birth(self):
        self.bridge.lookup=Mock(return_value={**self.dead,'retirement':'verified_finished'})
        with patch('agent_hub_host.agent.native_launch.read_tombstone',return_value={'created_at_ms':1000}):
            self.assertTrue(self.stop()['stopped'])

    def test_exit_without_returncode_is_not_confirmation(self):
        self.bridge.lookup=Mock(return_value={**self.dead,'exit':None})
        self.assertFalse(self.stop(100250)['stopped'])

    def test_expired_deadline_does_not_sleep(self):
        self.bridge.lookup=Mock(return_value=None)
        self.assertFalse(self.stop(99000)['stopped']);self.assertEqual(self.clock,0);self.bridge.native.assert_not_called()

    def test_mismatched_birth_never_confirms(self):
        self.bridge.lookup=Mock(return_value={**self.dead,'created_at_ms':999})
        with self.assertRaisesRegex(RuntimeError,'native_identity_mismatch'):self.stop()
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)

    def test_lookup_identity_mismatch_is_not_retried_or_confirmed(self):
        for field in ['id','tag','workspace']:
            with self.subTest(field=field):
                row={**self.dead,field:'wrong'}
                self.bridge.runner=Mock(return_value=types.SimpleNamespace(returncode=0,stdout=json.dumps(row)))
                self.bridge.executable='native'
                self.bridge.native=types.MethodType(Bridge.native,self.bridge)
                with self.assertRaisesRegex(RuntimeError,'native_identity_mismatch'):
                    Bridge.lookup(self.bridge,NATIVE,{'tag':self.live['tag'],'workspace':str(self.root)})
