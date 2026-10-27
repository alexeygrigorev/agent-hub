"""Terminal reports continue during blocked maintenance; replay stays fenced."""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_hub_host import private_fs, session_host, terminal_reports as terminal
from agent_hub_host.fleet import Dispatcher


class TerminalReportsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / 'repo'; self.ws.mkdir()
        self.offer = '11111111-1111-4111-8111-111111111111'
        self.native = '22222222-2222-4222-8222-222222222222'
        self.settings = dict(journal_dir=str(self.root / 'journal'), subscriptions=[],
                             launcher=dict(argv=['launcher'], root=str(self.root), config_dir=str(self.root)))
        self.posts = []
        def request(method, path, payload):
            self.posts.append(copy.deepcopy(payload))
            return dict(id=self.offer, state=payload['state'])
        self.runner = Mock(return_value=dict(state='completed-awaiting-review', resources_reserved=False,
                                            native_id=self.native))
        self.d = Dispatcher(self.settings, request=request, runner=self.runner)
        self.record = dict(offer_id=self.offer, task_id='hub-' + self.offer,
                           mode='headless', native_id=self.native, tag='task-hub-' + self.offer,
                           workspace=str(self.ws), target_cwd=str(self.ws), engine='opencode',
                           incarnation=int(time.time() * 1000) - 1000, state='running', settled=False,
                           result_path=str(self.ws / '.local/hub-results' / (self.offer + '.json')))
        self.d._active_launches[self.offer] = self.record
        self.d._persist_launch_record(self.record)
        self.result = Path(self.record['result_path']); self.result.parent.mkdir(parents=True)
        self.result.write_text(json.dumps(dict(status='done', report='Finished task.', artifacts=[])))
        tomb = dict(finished_at_ms=self.record['incarnation'] + 500,
                    exit=dict(code=0, signal=None, exited_at_ms=self.record['incarnation'] + 500))
        self.retired = patch('agent_hub_host.fleet.verify_native_retirement', return_value=(True, tomb, 'exact'))
        self.retired.start(); self.addCleanup(self.retired.stop)
        self.health = patch.object(Dispatcher, '_check_headless_health', side_effect=AssertionError('slow health called'))
        self.health.start(); self.addCleanup(self.health.stop)
        self.logs = patch.object(Dispatcher, '_retain_session_log', return_value=[])
        self.logs.start(); self.addCleanup(self.logs.stop)

    def read(self):
        return terminal.read_record(self.d, self.offer)

    def test_reports_during_blocked_maintenance_without_start_auth_or_health(self):
        # A real second Dispatcher reloads the durable row. The source's slow
        # cycle is deliberately unfinished when its report is acknowledged.
        entered, release = threading.Event(), threading.Event()
        def maintenance():
            entered.set(); release.wait(5)
            return []
        self.d.once = maintenance
        thread = session_host.maybe_start_maintenance_thread(None, self.d, Mock())
        self.assertTrue(entered.wait(1))
        try:
            started = time.monotonic()
            result = terminal.poll(self.d)
            self.assertLess(time.monotonic() - started, 2)
            self.assertTrue(thread.is_alive())
            self.assertEqual(result, [dict(id=self.offer, state='exited', reconciled=True)])
            self.assertEqual(self.posts[0]['report_status'], 'done')
            self.assertEqual(self.posts[0]['runtime']['exit_code'], 0)
            self.assertTrue(self.read()['settled'])
            self.assertTrue(all('reconcile' in call.args[0] for call in self.runner.call_args_list))
            self.assertEqual(terminal.poll(self.d), [])
            self.assertEqual(len(self.posts), 1)
        finally:
            release.set(); thread.join(2)

    def test_controller_schedules_delivery_while_its_actual_maintenance_is_blocked(self):
        entered, release, delivered = threading.Event(), threading.Event(), threading.Event()
        def maintenance(_):
            entered.set(); release.wait(5)
            return []
        def request(method, path, payload):
            ack = self.d.request(method, path, payload)
            delivered.set()
            return ack
        def factory(settings, **kwargs):
            kwargs.setdefault('request', request)
            kwargs.setdefault('runner', self.runner)
            return Dispatcher(settings, **kwargs)
        original_start = session_host.maybe_start_named_thread
        terminal_threads = []
        def start(current, target, args, name):
            if name != 'fleet-terminal-reports':
                return None
            thread = original_start(current, target, args, name)
            terminal_threads.append(thread)
            return thread
        def command(_):
            self.assertTrue(entered.wait(1))
            self.assertTrue(delivered.wait(2), 'terminal report waited behind maintenance')
            self.assertFalse(release.is_set())
        with ExitStack() as stack:
            stack.enter_context(patch.object(session_host.config, 'load_config', return_value=dict(machine_id='machine', access_token='synthetic')))
            stack.enter_context(patch.object(session_host.fleet, 'load_settings', return_value=self.settings))
            stack.enter_context(patch.object(session_host, 'qualify_sender_binding', return_value=SimpleNamespace(session_id='controller', workspace=str(self.ws))))
            stack.enter_context(patch.object(session_host.fleet, 'Dispatcher', side_effect=factory))
            stack.enter_context(patch.object(Dispatcher, 'once', maintenance))
            stack.enter_context(patch.object(session_host, 'QuotaCollector', return_value=Mock()))
            stack.enter_context(patch.object(session_host, 'maybe_start_named_thread', side_effect=start))
            stack.enter_context(patch.object(session_host.session_control, 'execute', side_effect=command))
            for name in ('status', 'metrics', 'disk', 'host_health', 'role_consumer', 'token'):
                stack.enter_context(patch.object(session_host, 'maybe_start_' + name + '_thread', return_value=None))
            try:
                session_host.run(str(self.root / 'settings.json'), self.settings['journal_dir'], cycles=1)
                self.assertTrue(delivered.is_set())
                self.assertEqual(len(self.posts), 1)
            finally:
                release.set()
                for thread in terminal_threads:
                    thread.join(3)
                    self.assertFalse(thread.is_alive())

    def test_live_native_and_reserved_children_never_deliver_from_result_hint(self):
        self.retired.stop()
        with patch('agent_hub_host.fleet.verify_native_retirement', return_value=(False, None, 'live')):
            self.assertEqual(terminal.poll(self.d), [])
        self.assertEqual(self.posts, [])
        self.retired.start()
        self.runner.return_value['resources_reserved'] = True
        self.assertEqual(terminal.poll(self.d), [])
        self.assertFalse(self.read()['settled'])
        self.assertEqual(self.posts, [])

    def test_missing_result_with_capture_hint_reports_failed_partial_runtime(self):
        self.result.unlink()  # This test owns its disposable result only.
        with patch('agent_hub_host.headless_output.retained', return_value=dict(capture_state='complete', log_tail='partial progress')):
            result = terminal.poll(self.d)
        self.assertEqual(result[0]['state'], 'failed')
        payload = self.posts[0]
        self.assertEqual(payload['report_status'], 'failed')
        self.assertEqual(payload['runtime']['exit_code'], 0)
        self.assertIn(payload['runtime']['outcome'], ('failed-with-partial-work', 'failed-without-report'))
        self.assertIn('partial progress', payload['runtime']['log_tail'])

    def test_lost_ack_restart_replays_identical_outbox_without_stale_overwrite(self):
        stale = copy.deepcopy(self.record)
        def lost(method, path, payload):
            self.posts.append(copy.deepcopy(payload))
            raise OSError('lost response')
        self.d.request = lost
        self.assertEqual(terminal.poll(self.d), [])
        pending = self.read()
        self.assertIn('terminal_payload', pending)
        with self.assertRaises(terminal.StaleLaunchRecord):
            self.d._persist_launch_record(stale)
        self.assertEqual(self.read(), pending)
        self.d.request = lambda method, path, payload: (self.posts.append(copy.deepcopy(payload)) or
                                                      dict(id=self.offer, state=payload['state']))
        self.assertEqual(terminal.poll(self.d)[0]['state'], 'exited')
        self.assertEqual(self.posts[0], self.posts[1])
        settled = self.read()
        with self.assertRaises(terminal.StaleLaunchRecord):
            self.d._persist_launch_record(stale)
        self.assertEqual(self.read(), settled)

    def test_all_worker_statuses_reach_terminal_delivery_with_runtime(self):
        for index, status in enumerate(('done', 'waiting-on-human', 'stuck', 'failed')):
            with self.subTest(status=status):
                offer = f'{index:08x}-4444-4444-8444-444444444444'
                record = copy.deepcopy(self.record)
                record.update(offer_id=offer, task_id='hub-' + offer, tag='task-hub-' + offer,
                              result_path=str(self.result.parent / (offer + '.json')))
                record.pop('_journal_revision', None)
                Path(record['result_path']).write_text(json.dumps(dict(status=status, report='Task result.')))
                self.d._persist_launch_record(record)
                # Each status has its own launch and matching host ACK.
                posts = []
                self.d.request = lambda method, path, payload: (posts.append(copy.deepcopy(payload)) or
                                                               dict(id=path.split('/')[-2], state=payload['state']))
                terminal.poll(self.d)
                payload = next(item for item in posts if item['launcher_task_id'] == 'hub-' + offer)
                self.assertEqual(payload['report_status'], status)
                self.assertEqual(payload['runtime']['outcome'], 'reported')
                self.assertEqual(payload['runtime']['exit_code'], 0)

    def test_wrong_ack_keeps_outbox_and_never_cleans_up(self):
        self.d.request = lambda *args: dict(id='foreign', state='exited')
        with patch.object(Dispatcher, '_maybe_cleanup_worktree') as cleanup:
            self.assertEqual(terminal.poll(self.d), [])
            cleanup.assert_not_called()
        self.assertFalse(self.read()['settled'])
        self.assertEqual(self.read()['terminal_payload']['report'], 'Finished task.')

    def test_bounded_parallel_batch_rotates_past_busy_candidates(self):
        for index in range(10):
            record = copy.deepcopy(self.record)
            offer = f'{index:08x}-3333-4333-8333-333333333333'
            record.update(offer_id=offer, task_id='hub-' + offer, tag='task-hub-' + offer)
            record.pop('_journal_revision', None)
            self.d._persist_launch_record(record)
        calls, active, maximum = [], 0, 0
        lock = threading.Lock()
        def reconcile(_, offer_ids, **kwargs):
            nonlocal active, maximum
            with lock:
                calls.extend(offer_ids); active += 1; maximum = max(maximum, active)
            try:
                time.sleep(0.01)
                return []
            finally:
                with lock: active -= 1
        with patch.object(Dispatcher, 'reconcile_launches', reconcile), patch.object(terminal, 'ready_hint', return_value=True):
            terminal.poll(self.d)
            self.assertEqual(len(calls), 8)
            terminal.poll(self.d)
        self.assertEqual(len(set(calls)), 11)
        self.assertLessEqual(maximum, 4)
        self.assertGreater(maximum, 1)
        self.assertEqual(active, 0)

    def test_foreign_identity_cannot_replace_journal_or_get_receipt(self):
        stale = copy.deepcopy(self.record); stale['incarnation'] += 1
        with self.assertRaises(ValueError):
            self.d._persist_launch_record(stale)
        self.d._active_launches[self.offer] = stale
        self.assertEqual(self.d.reconcile_launches(terminal_only=True), [])
        self.assertEqual(self.posts, [])
        self.assertEqual(self.read()['incarnation'], self.record['incarnation'])

    def test_busy_launch_skips_without_blocking_other_controller_threads(self):
        entered, release = threading.Event(), threading.Event()
        def holder():
            with terminal.launch_guard(self.d, self.offer):
                entered.set(); release.wait(5)
        thread = threading.Thread(target=holder); thread.start(); entered.wait(1)
        try:
            started = time.monotonic()
            self.assertEqual(terminal.poll(self.d), [])
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(self.posts, [])
        finally:
            release.set(); thread.join(2)
        self.assertTrue(terminal.poll(self.d))

    @unittest.skipIf(sys.platform == 'win32', 'POSIX subprocess lock proof')
    def test_independent_process_lock_fences_reconcile_and_persist(self):
        path = self.d._journal_launches_dir() / (self.offer + '.lock')
        code = "from agent_hub_host.private_fs import exclusive_lock; import sys;\nwith exclusive_lock(sys.argv[1]):\n print('ready',flush=True); sys.stdin.readline()"
        process = subprocess.Popen([sys.executable, '-c', code, str(path)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            self.assertEqual(terminal.poll(self.d), [])
            with self.assertRaises(BlockingIOError):
                self.d._persist_launch_record(copy.deepcopy(self.record))
            self.assertEqual(self.posts, [])
        finally:
            process.communicate('\n', timeout=3)
        self.assertEqual(process.returncode, 0)
        self.assertTrue(terminal.poll(self.d))

    def test_terminal_thread_is_single_inflight_and_does_not_block_commands(self):
        entered, release = threading.Event(), threading.Event()
        def blocked(_):
            entered.set(); release.wait(5)
            return []
        with patch('agent_hub_host.terminal_reports.poll', side_effect=blocked):
            thread = session_host.maybe_start_named_thread(None, session_host.terminal_step_once,
                                                         (self.d,), 'terminal-test')
            try:
                self.assertTrue(entered.wait(1))
                self.assertIs(session_host.maybe_start_named_thread(thread, session_host.terminal_step_once,
                                                                  (self.d,), 'terminal-test'), thread)
            finally:
                release.set(); thread.join(2)
