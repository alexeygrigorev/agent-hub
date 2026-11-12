import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from agent_hub_host.agent import Journal
from agent_hub_host.launch_reaper import initialize, remember, reap_expired_launches


class LaunchReaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Journal(Path(self.temp.name)/'journal.sqlite')
        self.addCleanup(self.journal.db.close)
        self.bridge = SimpleNamespace(journal=self.journal, _inflight=set(), _inflight_lock=threading.Lock())

    def row(self, state, deadline=None, native=None, dispatch=None, payload=None):
        with self.journal.db as db:
            db.execute('INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,runtime_deadline,native_dispatch_started,legacy_failure_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       ('operation','session',1,'digest','/owned','task-hub2-session',native,state,deadline,dispatch,json.dumps(payload) if payload else None))
        remember(self.journal,'operation',1000)

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
        evidence={'source':'aplexer_all_registry_census','complete':True,'match_count':0}
        self.bridge.launcher=SimpleNamespace(inventory=lambda:SimpleNamespace(fresh=lambda:True,absence_evidence=lambda *_:evidence))
        reap_expired_launches(self.bridge,2000)
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
        reap_expired_launches(self.bridge,2000)
        self.assertEqual(self.state(),'aborting')
