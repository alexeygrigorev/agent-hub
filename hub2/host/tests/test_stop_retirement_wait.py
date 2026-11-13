"""A stop confirms exact native death even when retirement trails kill."""
import json
import hashlib
import subprocess
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from hub2_agent_hub_host.agent import Bridge, Journal
from hub2_agent_hub_host.native_launch import canonical

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
        self.bridge.resolve_launch_native=lambda row, **kwargs:(NATIVE,self.live)
        self.bridge.native=Mock()
        self.clock=0
        self.wall=100
        def sleep(seconds):self.clock+=seconds
        self.patches=[patch('hub2_agent_hub_host.agent.time.monotonic',side_effect=lambda:self.clock),patch('hub2_agent_hub_host.agent.time.time',side_effect=lambda:self.wall+self.clock),patch('hub2_agent_hub_host.agent.time.sleep',side_effect=sleep)]
        for p in self.patches:p.start();self.addCleanup(p.stop)

    def stop(self,deadline=102000):
        return self.bridge.command({'id':'stop-command','session_id':'session','epoch':1,'kind':'stop','deadline_ms':deadline})

    def test_delayed_qualified_retirement_is_confirmed_and_replayed_without_kill(self):
        self.bridge.lookup=Mock(side_effect=[self.live,None,self.dead])
        result=self.stop();self.assertTrue(result['stopped']);self.assertEqual(result['native_id'],NATIVE)
        self.assertEqual(self.bridge.lookup.call_count,3)
        self.assertEqual(self.stop(),result);self.bridge.native.assert_called_once_with('kill',NATIVE,timeout=2.0)
        ack=json.loads(self.journal.db.execute('SELECT payload FROM outbox').fetchone()[0]);self.assertEqual(ack['result'],result)

    def test_no_evidence_is_never_success_and_wait_stays_within_deadline(self):
        self.bridge.lookup=Mock(return_value=None)
        result=self.stop(100250);self.assertFalse(result['stopped']);self.assertLessEqual(self.clock,.251)
        self.assertGreater(self.bridge.lookup.call_count,1)

    def test_qualified_retirement_checks_original_birth(self):
        self.bridge.lookup=Mock(return_value={**self.dead,'retirement':'verified_finished'})
        with patch('hub2_agent_hub_host.agent.native_launch.read_tombstone',return_value={'created_at_ms':1000}):
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

    def test_late_status_evidence_is_refused(self):
        def status(*args, **kwargs):
            self.assertLessEqual(kwargs['timeout'], .25)
            self.clock += 10
            return self.dead
        self.bridge.lookup = Mock(side_effect=status)
        self.assertFalse(self.stop(100250)['stopped'])
        self.assertFalse(self.stop(100250)['stopped'])
        self.bridge.native.assert_called_once()

    def test_initial_resolution_and_kill_share_the_budget(self):
        def resolve(row, **kwargs):
            self.assertEqual(kwargs['timeout'], .25)
            self.clock += .1
            return NATIVE, self.live
        def kill(*args, **kwargs):
            self.assertAlmostEqual(kwargs['timeout'], .15)
            self.clock += .2
        self.bridge.resolve_launch_native = Mock(side_effect=resolve)
        self.bridge.native = Mock(side_effect=kill)
        self.bridge.lookup = Mock(return_value=self.dead)
        self.assertFalse(self.stop(100250)['stopped'])
        self.bridge.lookup.assert_not_called()

    def test_slow_initial_resolution_never_kills(self):
        def resolve(row, **kwargs):
            self.clock += 10
            return NATIVE, self.live
        self.bridge.resolve_launch_native = Mock(side_effect=resolve)
        self.assertFalse(self.stop(100250)['stopped'])
        self.bridge.native.assert_not_called()

    def test_timeout_is_journaled_without_repeating_native_effects(self):
        self.bridge.native = Mock(side_effect=subprocess.TimeoutExpired('kill', .25))
        result = self.stop(100250)
        self.assertFalse(result['stopped'])
        self.assertEqual(self.stop(100250), result)
        self.bridge.native.assert_called_once()

    def test_five_second_monotonic_cap_survives_backward_wall_clock(self):
        def status(*args, **kwargs):
            self.assertLessEqual(kwargs['timeout'], 5)
            self.clock += 6
            return self.dead
        self.bridge.lookup = Mock(side_effect=status)
        with patch('hub2_agent_hub_host.agent.time.time', return_value=100):
            self.assertFalse(self.stop(160000)['stopped'])

    def test_forward_wall_clock_invalidates_read_before_success(self):
        def status(*args, **kwargs):
            self.wall = 200
            return self.dead
        self.bridge.lookup = Mock(side_effect=status)
        self.assertFalse(self.stop()['stopped'])

    def test_initial_known_native_lookup_receives_timeout(self):
        self.bridge.resolve_launch_native = types.MethodType(Bridge.resolve_launch_native, self.bridge)
        self.bridge.lookup = Mock(side_effect=[self.live, self.dead])
        self.assertTrue(self.stop(100250)['stopped'])
        self.assertEqual(self.bridge.lookup.call_args_list[0].kwargs, {'timeout': .25})

    def test_legacy_completed_receipt_accepts_only_added_deadline(self):
        legacy = {'id':'stop-command','session_id':'session','epoch':1,'kind':'stop'}
        self.bridge.lookup = Mock(return_value=self.dead)
        result = self.bridge.command(legacy)
        self.assertTrue(result['stopped'])
        self.assertEqual(self.bridge.command({**legacy, 'deadline_ms':102000}), result)
        self.bridge.native.assert_called_once_with('kill', NATIVE)
        for changed in [{'epoch':2}, {'session_id':'other'}, {'text':'extra'}, {'kind':'send'}]:
            with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'command_id_conflict'):
                self.bridge.command({**legacy, 'deadline_ms':102000, **changed})

    def test_legacy_executing_receipt_remains_uncertain_without_kill(self):
        legacy = {'id':'stop-command','session_id':'session','epoch':1,'kind':'stop'}
        digest = hashlib.sha256(canonical(legacy).encode()).hexdigest()
        with self.journal.db:
            self.bridge.begin_command('stop-command', digest)
        with self.assertRaisesRegex(RuntimeError, 'command_outcome_uncertain'):
            self.bridge.command({**legacy, 'deadline_ms':102000})
        self.bridge.native.assert_not_called()

    def test_changed_existing_deadline_is_still_a_conflict(self):
        self.bridge.lookup = Mock(return_value=self.dead)
        self.assertTrue(self.stop()['stopped'])
        with self.assertRaisesRegex(ValueError, 'command_id_conflict'):
            self.stop(103000)
