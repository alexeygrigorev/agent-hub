import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from hub2_agent_hub_host.agent import Journal
from hub2_agent_hub_host.launch_reaper import initialize, remember, reap_expired_launches, observation_thread


class LaunchReaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(Path(self.temp.name)/'journal.sqlite')
        self.addCleanup(self.journal.db.close)
        self.bridge = SimpleNamespace(journal=self.journal, _inflight=set(), _inflight_lock=threading.Lock())
        def join_census():
            thread = observation_thread(self.bridge)
            if thread is not None:
                thread.join(5)
        self.addCleanup(join_census)

    def row(self, state, deadline=None, native=None, dispatch=None, payload=None):
        with self.journal.db as db:
            db.execute('INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,runtime_deadline,native_dispatch_started,legacy_failure_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       ('operation','session',1,'digest','/owned','task-hub2-session',native,state,deadline,dispatch,json.dumps(payload) if payload else None))
        remember(self.journal,'operation',1000)

    def settle(self, now=2000):
        """Sweep, let the single background census finish, sweep again with its cache."""
        reap_expired_launches(self.bridge, now)
        thread = observation_thread(self.bridge)
        if thread is not None:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        reap_expired_launches(self.bridge, now)

    def state(self):
        return self.journal.db.execute('SELECT state FROM launches').fetchone()[0]

    def receipt(self):
        return json.loads(self.journal.db.execute('SELECT payload FROM outbox').fetchone()[0])

    def test_expired_admission_is_terminal_and_reports_no_dispatch_atomically(self):
        self.row('pending_admission')
        reap_expired_launches(self.bridge,999)
        self.assertEqual(self.state(),'pending_admission')
        reap_expired_launches(self.bridge,1000)
        self.assertEqual(self.state(),'stopped_before_dispatch')
        self.assertTrue(self.receipt()['no_dispatch'])
        self.assertTrue(self.receipt()['admission_expired'])
        reap_expired_launches(self.bridge,2000)
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],1)

    def test_inflight_and_dispatched_launches_cannot_be_reaped_as_undispatched(self):
        self.row('pending_admission',dispatch=1)
        reap_expired_launches(self.bridge,2000)
        self.assertEqual(self.state(),'pending_admission')
        self.journal.db.execute('UPDATE launches SET native_dispatch_started=0')
        self.journal.db.commit()
        self.bridge._inflight.add('session')
        reap_expired_launches(self.bridge,2000)
        self.assertEqual(self.state(),'pending_admission')

    def test_failed_unconfirmed_replays_frozen_terminal_report_after_deadline(self):
        payload={'operation_id':'operation','epoch':1,'native_outcome':'unconfirmed','evidence':{'frozen':True}}
        self.row('launch_failed_unconfirmed',deadline=2000,payload=payload)
        reap_expired_launches(self.bridge,1999)
        self.assertEqual(self.state(),'launch_failed_unconfirmed')
        reap_expired_launches(self.bridge,2000)
        self.assertEqual(self.state(),'launch_failed_reported')
        self.assertEqual(self.receipt(),payload)
        self.assertNotIn('native_exit_confirmed',self.receipt())

    def test_upgrade_window_is_persisted_and_not_renewed_on_restart(self):
        self.row('pending_admission')
        self.journal.db.execute('DELETE FROM launch_windows')
        self.journal.db.commit()
        initialize(self.journal)
        until=self.journal.db.execute('SELECT admission_expires_at FROM launch_windows').fetchone()[0]
        initialize(self.journal)
        self.assertEqual(self.journal.db.execute('SELECT admission_expires_at FROM launch_windows').fetchone()[0],until)

    def test_native_aborting_requires_actual_exit_evidence(self):
        self.row('aborting',deadline=100,native='native',dispatch=1)
        reap_expired_launches(self.bridge,2000)
        self.assertEqual(self.state(),'aborting')
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)

    def test_overdue_aborting_launch_absent_from_fresh_registry_reports_unconfirmed(self):
        self.row('aborting',deadline=100,native='native',dispatch=1)
        evidence={'source':'aplexer_all_registry_census','complete':True,'match_count':0,'started_at_ms':150,'observed_at_ms':160}
        self.bridge.launcher=SimpleNamespace(inventory=lambda:SimpleNamespace(fresh=lambda:True,absence_evidence=lambda *_:evidence))
        self.settle()
        self.assertEqual(self.state(),'expired_unconfirmed')
        payload=self.receipt()
        self.assertEqual(payload['native_outcome'],'unconfirmed')
        self.assertTrue(payload['launch_expired'])
        self.assertEqual(payload['evidence']['dispatch_provenance'],'host_journal')
        self.assertNotIn('native_exit_confirmed',payload)
        self.assertNotIn('exit_code',payload)

    def test_registry_presence_preserves_native_containment_work(self):
        self.row('aborting',deadline=100,native='native',dispatch=1)
        self.bridge.launcher=SimpleNamespace(inventory=lambda:SimpleNamespace(fresh=lambda:True,absence_evidence=lambda *_:None))
        self.settle()
        self.assertEqual(self.state(),'aborting')

    def test_census_timeout_keeps_launch_unresolved_without_aborting_reconciliation(self):
        self.row('aborting',deadline=100,native='native',dispatch=1)
        def timeout():
            raise subprocess.TimeoutExpired(['a','list','--all','--json'],5)
        self.bridge.launcher=SimpleNamespace(inventory=timeout)
        with self.assertLogs('hub2_agent_hub_host.launch_reaper',level='WARNING'):
            self.settle()
        self.assertEqual(self.state(),'aborting')
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)

    def many_overdue(self, count):
        with self.journal.db as db:
            for i in range(count):
                db.execute('INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,runtime_deadline,native_dispatch_started) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (f'op{i}',f'session{i}',1,'digest',f'/owned/{i}',f'task-hub2-session{i}',f'native{i}','aborting',100,1))
                db.execute('INSERT INTO launch_windows VALUES(?,?)',(f'op{i}',1000))

    def test_many_overdue_rows_share_one_nonblocking_failed_observation(self):
        self.many_overdue(1000)
        calls, release = [], threading.Event()
        def blocked():
            calls.append(time.monotonic())
            release.wait(10)
            raise subprocess.TimeoutExpired(['a','list','--all','--json'],5)
        self.bridge.launcher=SimpleNamespace(inventory=blocked)
        started=time.monotonic()
        for _ in range(3):
            reap_expired_launches(self.bridge,2000)
        self.assertLess(time.monotonic()-started,1)  # census still blocked; control loop never waits
        thread=observation_thread(self.bridge)
        self.assertTrue(thread.is_alive())
        self.assertEqual(len(calls),1)  # single in-flight, not once per row or sweep
        release.set()
        with self.assertLogs('hub2_agent_hub_host.launch_reaper',level='WARNING'):
            thread.join(5)
        for _ in range(3):
            reap_expired_launches(self.bridge,2000)  # inside the failure backoff
        self.assertEqual(len(calls),1)
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM launches WHERE state='aborting'").fetchone()[0],1000)
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)

    def test_background_census_settles_every_absent_row_with_unconfirmed_evidence(self):
        self.many_overdue(3)
        calls=[]
        def inventory():
            calls.append(1)
            return SimpleNamespace(fresh=lambda:True,absence_evidence=lambda w,t:{'source':'aplexer_all_registry_census','complete':True,'match_count':0,'tag':t,'started_at_ms':150,'observed_at_ms':160})
        self.bridge.launcher=SimpleNamespace(inventory=inventory)
        self.settle()
        self.assertEqual(len(calls),1)
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM launches WHERE state='expired_unconfirmed'").fetchone()[0],3)
        payloads=[json.loads(r[0]) for r in self.journal.db.execute('SELECT payload FROM outbox')]
        self.assertEqual(len(payloads),3)
        for payload in payloads:
            self.assertEqual(payload['native_outcome'],'unconfirmed')
            self.assertNotIn('native_exit_confirmed',payload)
            self.assertNotIn('exit_code',payload)

    def test_stale_census_is_never_applied(self):
        self.many_overdue(1)
        inventory=SimpleNamespace(fresh=lambda:False,absence_evidence=lambda *_:{'complete':True})
        self.bridge.launcher=SimpleNamespace(inventory=lambda:inventory)
        self.settle()
        self.assertEqual(self.journal.db.execute("SELECT COUNT(*) FROM launches WHERE state='aborting'").fetchone()[0],1)

    def test_blocked_census_does_not_hold_inflight_lock_or_delay_other_rows(self):
        self.many_overdue(2)
        self.row('pending_admission')
        release=threading.Event()
        self.bridge.launcher=SimpleNamespace(inventory=lambda:release.wait(10))
        self.addCleanup(release.set)
        started=time.monotonic()
        reap_expired_launches(self.bridge,2000)
        self.assertLess(time.monotonic()-started,1)
        self.assertEqual(self.state_of('operation'),'stopped_before_dispatch')  # admission rows settle without census
        self.assertTrue(self.bridge._inflight_lock.acquire(timeout=.1))  # STOP/cleanup can take it
        self.bridge._inflight_lock.release()

    def sequenced(self, starts):
        calls = []
        def inventory():
            started = starts[min(len(calls), len(starts)-1)]
            calls.append(started)
            return SimpleNamespace(fresh=lambda:True, absence_evidence=lambda w,t:{
                'source':'aplexer_all_registry_census','complete':True,'match_count':0,'tag':t,
                'started_at_ms':started,'observed_at_ms':started+10})
        self.bridge.launcher = SimpleNamespace(inventory=inventory)
        return calls

    def wait_census(self):
        thread = observation_thread(self.bridge)
        if thread is not None:
            thread.join(5)
            self.assertFalse(thread.is_alive())

    def test_fresh_census_started_before_deadline_is_never_applied_then_replaced(self):
        self.row('aborting',deadline=100,native='native',dispatch=1)
        calls = self.sequenced([50, 150])  # first (still fresh) census began before the deadline
        reap_expired_launches(self.bridge,2000); self.wait_census()
        self.assertEqual(self.state(),'aborting')  # cached pre-deadline census rejected by the worker
        self.assertEqual(self.journal.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0],0)
        reap_expired_launches(self.bridge,2000)  # cached pre-deadline census triggers a replacement
        self.wait_census()
        self.assertEqual(calls,[50,150])
        self.assertEqual(self.state(),'expired_unconfirmed')
        self.assertGreaterEqual(self.receipt()['evidence']['started_at_ms'],100)

    def test_deadline_crossed_during_collection_keeps_row_until_qualifying_census(self):
        self.row('aborting',deadline=100,native='native',dispatch=1)
        calls = self.sequenced([99, 100])  # collection began 1ms before the deadline, then exactly at it
        reap_expired_launches(self.bridge,2000); self.wait_census()
        self.assertEqual(self.state(),'aborting')
        reap_expired_launches(self.bridge,2000); self.wait_census()
        self.assertEqual(calls,[99,100])
        self.assertEqual(self.state(),'expired_unconfirmed')
        self.assertEqual(self.receipt()['evidence']['started_at_ms'],100)

    def test_malformed_census_start_is_never_applied(self):
        self.row('aborting',deadline=100,native='native',dispatch=1)
        self.bridge.launcher=SimpleNamespace(inventory=lambda:SimpleNamespace(fresh=lambda:True,absence_evidence=lambda *_:{'complete':True}))
        reap_expired_launches(self.bridge,2000); self.wait_census()
        self.assertEqual(self.state(),'aborting')

    def state_of(self, operation):
        return self.journal.db.execute('SELECT state FROM launches WHERE operation_id=?',(operation,)).fetchone()[0]


class BridgeResponsivenessTests(unittest.TestCase):
    def test_reconcile_stays_responsive_while_census_is_blocked_for_many_rows(self):
        from hub2_agent_hub_host.agent import Bridge
        with tempfile.TemporaryDirectory() as temp:
            journal = Journal(Path(temp)/'journal.sqlite')
            release = threading.Event()
            calls = []
            def blocked():
                calls.append(1)
                release.wait(10)
                raise subprocess.TimeoutExpired(['a','list','--all','--json'],5)
            bridge = Bridge(SimpleNamespace(), journal, [temp], ['codex'], executable='python3')
            bridge.launcher = SimpleNamespace(inventory=blocked)
            with journal.db as db:
                for i in range(200):
                    # Half are overdue runtime rows, half legacy undispatched aborts.
                    legacy = i % 2
                    db.execute('INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,runtime_deadline,native_dispatch_started) VALUES(?,?,?,?,?,?,?,?,?,?)',
                               (f'op{i}',f'session{i}',1,'digest',f'/owned/{i}',f'task-hub2-session{i}',None if legacy else f'native{i}','aborting',100,None if legacy else 1))
                    db.execute('INSERT OR REPLACE INTO launch_windows VALUES(?,?)',(f'op{i}',1000))
            try:
                started = time.monotonic()
                for _ in range(3):
                    try:
                        bridge.reconcile()
                    except AttributeError:
                        pass  # Other launcher surface is outside this fixture; census must still not block.
                self.assertLess(time.monotonic()-started, 1)
                self.assertEqual(len(calls), 1)
                self.assertTrue(bridge._inflight_lock.acquire(timeout=.1))  # STOP/inflight cleanup can proceed
                bridge._inflight_lock.release()
            finally:
                release.set()
                thread = observation_thread(bridge)
                if thread is not None:
                    thread.join(5)
                journal.close_thread()
                journal.db.close()
