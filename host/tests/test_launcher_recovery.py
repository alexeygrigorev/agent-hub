"""Never-started proof must fence spawn, survive receipt loss, and fail closed."""
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_hub_host.launcher_recovery import REASON, settle_unstarted
from agent_hub_host.fleet import Dispatcher


@unittest.skipUnless(os.name == 'posix', 'POSIX launcher lock contract')
class LauncherRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'ws'
        self.ws.mkdir(mode=0o700)
        (self.ws / '.local').mkdir(mode=0o700)
        self.cfg = self.root / 'cfg'
        self.cfg.mkdir(mode=0o700)
        self.launcher = {'config_dir': str(self.cfg), 'root': str(self.root), 'argv': ['launcher']}
        self.task = 'hub-11111111-1111-4111-8111-111111111111'
        self.offer = self.task[4:]
        self.db = self.cfg / 'state.db'
        with sqlite3.connect(self.db) as c:
            c.executescript('''CREATE TABLE tasks (id TEXT PRIMARY KEY, state TEXT, payload TEXT,
                idempotency_key TEXT, reason TEXT, reviewer TEXT, updated_at TEXT);
                CREATE TABLE task_native_bindings (task_id TEXT, native_id TEXT);''')
            c.execute('INSERT INTO tasks VALUES (?, ?, ?, ?, NULL, NULL, NULL)',
                      (self.task, 'queued', json.dumps({'owner': 'hub-fleet', 'cwd': str(self.ws)}), self.task))

    def settle(self):
        return settle_unstarted(self.launcher, self.task, self.ws, self.ws)

    def state(self):
        with sqlite3.connect(self.db) as c:
            return c.execute('SELECT state, reason FROM tasks').fetchone()

    def test_fences_delayed_launcher_without_deleting_payload_or_reservation(self):
        self.assertTrue(self.settle())
        self.assertEqual(self.state(), ('failed', REASON))
        with sqlite3.connect(self.db) as c:
            # The maintained launcher's queued->starting CAS cannot now spawn.
            self.assertEqual(c.execute("UPDATE tasks SET state='starting' WHERE id=? AND state='queued'", (self.task,)).rowcount, 0)
            self.assertEqual(json.loads(c.execute('SELECT payload FROM tasks').fetchone()[0])['owner'], 'hub-fleet')
        self.assertTrue(self.settle())  # retry after lost Hub receipt

    def test_live_uncertain_and_unrelated_failed_tasks_never_settled(self):
        for state in ('starting', 'running', 'launch-uncertain', 'stalled', 'failed'):
            with self.subTest(state=state):
                with sqlite3.connect(self.db) as c:
                    c.execute('UPDATE tasks SET state=?', (state,))
                self.assertFalse(self.settle())
                self.assertEqual(self.state()[0], state)

    def test_any_native_binding_or_start_artifact_blocks_proof(self):
        with sqlite3.connect(self.db) as c:
            c.execute('INSERT INTO task_native_bindings VALUES (?, ?)', (self.task, 'native'))
        self.assertFalse(self.settle())
        with sqlite3.connect(self.db) as c:
            c.execute('DELETE FROM task_native_bindings')
        for name in (f'launch-{self.task}-start.json', f'first-action-{self.task}.json'):
            path = self.ws / '.local' / name
            path.symlink_to(self.ws / 'missing')  # broken links also count as evidence
            self.assertFalse(self.settle())
            path.unlink()
        self.assertEqual(self.state()[0], 'queued')

    def test_contended_launch_lock_never_releases_capacity(self):
        import fcntl
        with (self.cfg / 'launch.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertFalse(self.settle())
        self.assertEqual(self.state()[0], 'queued')

    def test_identity_mismatch_and_unknown_database_fail_closed(self):
        self.assertFalse(settle_unstarted(self.launcher, self.task, self.ws, self.root))
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE tasks SET idempotency_key='other'")
        self.assertFalse(self.settle())
        self.db.unlink()
        self.assertFalse(self.settle())
        self.assertFalse(self.db.exists())

    def test_receipt_retried_after_dispatcher_restart_with_revalidated_proof(self):
        self.assertTrue(self.settle())
        record = {'offer_id': self.offer, 'task_id': self.task, 'mode': 'headless',
                  'workspace': str(self.ws), 'target_cwd': str(self.ws),
                  'state': 'not_started', 'reason': 'launcher_not_started', 'settled': False}
        journal = self.root / 'journal'
        (journal / 'fleet-launches').mkdir(parents=True, mode=0o700)
        entry = journal / 'fleet-launches' / (self.offer + '.json')
        entry.write_text(json.dumps(record))
        entry.chmod(0o600)
        calls = []
        settings = {'launcher': self.launcher, 'workspaces': {'main': str(self.ws)}, 'subscriptions': []}
        dispatcher = Dispatcher(settings, request=lambda *args: calls.append(args) or {}, journal_dir=str(journal))
        result = dispatcher.reconcile_launches()
        self.assertEqual(result[0]['state'], 'failed')
        self.assertEqual(calls[0][2], {'state': 'failed', 'launcher_task_id': self.task, 'confirmed_exit': True, 'reason': 'launcher_not_started'})
        self.assertTrue(json.loads(entry.read_text())['settled'])
        self.assertEqual(dispatcher.reconcile_launches(), [])

    def test_forged_journal_cannot_settle_native_start(self):
        self.test_receipt_retried_after_dispatcher_restart_with_revalidated_proof()
        journal = self.root / 'journal'
        entry = journal / 'fleet-launches' / (self.offer + '.json')
        record = json.loads(entry.read_text())
        record['settled'] = False
        entry.write_text(json.dumps(record))
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE tasks SET state='running'")
        calls = []
        dispatcher = Dispatcher({'launcher': self.launcher}, request=lambda *a: calls.append(a), journal_dir=str(journal))
        self.assertEqual(dispatcher.reconcile_launches(), [])
        self.assertEqual(calls, [])

    def test_failed_run_settles_only_proven_queued_task(self):
        import datetime
        requests = []
        sub = {'service': 'go', 'account_label': 'muse', 'engine': 'opencode',
               'launcher_provider': 'opencode', 'launcher_model': 'opencode-go/muse-spark-1.3-contributor',
               'admission_argv': ['gate']}
        settings = {'launcher': self.launcher, 'workspaces': {'main': str(self.ws)}, 'subscriptions': [sub]}
        def runner(argv, **kwargs):
            if argv[0] == 'gate':
                return {'launch_allowed': True, 'profile_supported': True, 'account_label': 'muse',
                        'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
            if 'plan' in argv:
                return {'eligible': [{'provider': 'opencode', 'model': sub['launcher_model'], 'task_fit': 1.0}]}
            if 'run' in argv:
                raise RuntimeError('admission failed')
            return {}
        offer = {'id': self.offer, 'engine': 'opencode', 'account': 'muse', 'mode': 'headless', 'workspace': 'main', 'prompt': 'review'}
        dispatcher = Dispatcher(settings, runner=runner, request=lambda *a: requests.append(a) or {})
        with patch.object(dispatcher, '_persist_launch_record', side_effect=OSError('journal unavailable')):
            self.assertEqual(dispatcher.dispatch(offer)['state'], 'uncertain')
        self.assertEqual(self.state()[0], 'queued')
        self.assertEqual(dispatcher.dispatch(offer)['state'], 'failed')
        self.assertEqual(self.state(), ('failed', REASON))
        self.assertEqual(requests[-1][2]['reason'], 'launcher_not_started')
        with sqlite3.connect(self.db) as c:
            c.execute("UPDATE tasks SET state='starting'")
        self.assertEqual(dispatcher.dispatch(offer)['state'], 'uncertain')
        self.assertEqual(requests[-1][2], {'state': 'uncertain'})


@unittest.skipUnless(os.name == 'posix', 'POSIX launcher lock contract')
class LegacyRetirementTests(unittest.TestCase):
    def test_exact_retirement_releases_only_its_reservation_without_inventing_success(self):
        from agent_hub_host.launcher_recovery import settle_retired_legacy
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); cfg = root / 'cfg'; cfg.mkdir(mode=0o700)
            native = root / 'native'; (native / 'retired-sessions').mkdir(parents=True, mode=0o700)
            (native / 'sessions').mkdir(mode=0o700)
            task = 'hub-11111111-1111-4111-8111-111111111111'
            sid = '22222222-2222-4222-8222-222222222222'
            retired = native / 'retired-sessions' / sid; retired.mkdir(mode=0o700)
            tomb = {'id': sid, 'tag': 'task-' + task, 'workspace': '/removed/workspace', 'cause': 'finished', 'finished_at_ms': 1790000000000}
            leaf = retired / 'tombstone.json'
            leaf.write_text(json.dumps(tomb)); leaf.chmod(0o600)
            db = cfg / 'state.db'
            with sqlite3.connect(db) as c:
                c.executescript('CREATE TABLE tasks (id TEXT,state TEXT,payload TEXT,created_at TEXT,idempotency_key TEXT,reason TEXT,reviewer TEXT,updated_at TEXT); CREATE TABLE task_native_bindings (task_id TEXT);')
                c.execute('INSERT INTO tasks VALUES (?,?,?,?,?,NULL,NULL,NULL)', (task, 'running', json.dumps({'owner': 'hub-fleet', 'cwd': '/removed/workspace'}), '2026-09-01 00:00:00', task))
            def settle(): return settle_retired_legacy({'config_dir': str(cfg)}, state_dir=native)
            for bad in ({'workspace': '/wrong'}, {'tag': 'task-other'}, {'cause': 'unknown'}, {'finished_at_ms': 1}, {'finished_at_ms': 9999999999999}, {'id': 'other'}):
                leaf.write_text(json.dumps({**tomb, **bad})); self.assertEqual(settle(), [])
            leaf.write_text(json.dumps(tomb))
            live = native / 'sessions' / sid; live.mkdir(mode=0o700)
            self.assertEqual(settle(), [])
            live.rmdir()
            reused = native / 'sessions' / '33333333-3333-4333-8333-333333333333'
            reused.mkdir(mode=0o700)
            record = reused / 'session.json'
            record.write_text(json.dumps({'tag': tomb['tag']}))
            self.assertEqual(settle(), [])
            record.unlink(); reused.rmdir()
            self.assertEqual(settle(), [{'task_id': task, 'native_id': sid, 'state': 'failed'}])
            self.assertEqual(settle(), [])
            with sqlite3.connect(db) as c:
                self.assertEqual(c.execute('SELECT state FROM tasks').fetchone()[0], 'failed')
                self.assertEqual(c.execute('SELECT count(*) FROM task_native_bindings').fetchone()[0], 0)
