"""Host start contracts: local profiles, early receipts and a single socket reader."""
import json
import queue
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from agent_hub_host.agent import Bridge, Journal
from agent_hub_host.client import ApiError
from agent_hub_host.native_launch import NativeLauncher, LOCAL_ROUTES


class StopServe(BaseException):
    pass


class NonblockingLaunchTests(unittest.TestCase):
    def test_poison_receipt_is_preserved_and_does_not_block_socket_or_later_receipts(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Journal(Path(temp) / 'journal.sqlite')
            journal.emit('poison', '/api/sessions/stopped/finish', {'native_exit_confirmed': True})
            journal.emit('good', '/api/sessions/live/started', {'native_id': 'native'})
            delivered = []
            def request(method, path, body, key):
                if key == 'poison':
                    raise ApiError('Hub HTTP 409', 409, 'stale_session_fence')
                delivered.append(key)
                return {}
            socket = Mock()
            socket.__enter__ = Mock(return_value=socket)
            socket.__exit__ = Mock(return_value=False)
            socket.recv.side_effect = StopServe()
            bridge = Bridge.__new__(Bridge)
            bridge.client = SimpleNamespace(socket=lambda _: socket, request=request)
            bridge.journal = journal
            bridge.budget_paused_until = bridge.budget_safety_paused_until = 0
            bridge.heartbeat, bridge.reconcile = Mock(), Mock()
            try:
                with self.assertLogs('agent_hub_host.agent', level='ERROR') as logs:
                    with self.assertRaises(StopServe):
                        bridge.serve('machine', [])
                self.assertTrue(socket.recv.called, 'poison receipt prevented socket processing')
                self.assertEqual(delivered, ['good'])
                self.assertFalse(journal.db.execute('SELECT 1 FROM outbox').fetchone())
                dead = journal.db.execute('SELECT * FROM outbox_dead_letters').fetchone()
                self.assertEqual(dead['key'], 'poison')
                self.assertEqual(dead['status'], 409)
                self.assertEqual(dead['reason'], 'stale_session_fence')
                self.assertEqual(json.loads(dead['payload']), {'native_exit_confirmed': True})
                self.assertIn('stale_session_fence', logs.output[0])
            finally:
                journal.close_thread()

    def test_auth_budget_timeout_and_server_errors_remain_retryable(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Journal(Path(temp) / 'journal.sqlite')
            try:
                for status in (401, 403, 408, 429, 500, 503):
                    with self.subTest(status=status):
                        journal.emit('retry', '/api/sessions/s/finish', {})
                        client = SimpleNamespace(request=Mock(side_effect=ApiError('retry', status)))
                        with self.assertRaises(ApiError):
                            journal.flush(client)
                        self.assertTrue(journal.db.execute('SELECT 1 FROM outbox').fetchone())
                        self.assertFalse(journal.db.execute('SELECT 1 FROM outbox_dead_letters').fetchone())
            finally:
                journal.close_thread()

    def test_journal_heartbeat_thread_can_read_and_emit_without_shared_transaction(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Journal(Path(temp) / 'journal.sqlite')
            errors = []
            def worker():
                try:
                    journal.emit('thread', '/api/sessions/s/started', {'native_id': 'n'})
                    self.assertEqual(journal.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
                except Exception as error:
                    errors.append(error)
                finally:
                    journal.close_thread()
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertFalse(errors)
            self.assertEqual(journal.db.execute('SELECT key FROM outbox').fetchone()[0], 'thread')
            journal.close_thread()

    def test_every_local_headless_route_returns_native_id_before_first_action(self):
        with tempfile.TemporaryDirectory() as temp:
            for engine, route in LOCAL_ROUTES.items():
                with self.subTest(engine=engine):
                    calls = []
                    def runner(argv, **options):
                        calls.append(argv)
                        return subprocess.CompletedProcess(argv, 0, json.dumps({
                            'id': 'd6cf9872-8597-48a5-be64-990cdef4db31', 'tag': 'task-hub2-s',
                            'workspace': temp, 'command': argv[argv.index('--') + 1:],
                        }), '')
                    launcher = NativeLauncher({}, runner=runner)
                    launcher.wait_first_action = Mock(side_effect=AssertionError('start must not wait for a model'))
                    model = 'opencode-go/muse-spark-1.3-contributor' if engine == 'go' else 'test-model'
                    receipt = launcher.launch('hub2-s', {'session_id': 's', 'workspace': temp, 'engine': engine,
                        'model': model, 'effort': None, 'prompt': 'Work', 'timeout_seconds': 600})
                    self.assertFalse(receipt['first_action_verified'])
                    self.assertEqual(receipt['session_id'], 'd6cf9872-8597-48a5-be64-990cdef4db31')
                    argv = calls[0]
                    self.assertIn('hub.managed=true', argv)
                    self.assertIn('hub.session=s', argv)
                    child = argv[argv.index('--') + 1:]
                    self.assertEqual(child[:3], ['a', 'task', 'run'])
                    self.assertIn(route.get('profile', route.get('engine')), child)
                    self.assertIn('--engine-arg=--model', child)
                    self.assertEqual(Path(child[child.index('--prompt-file') + 1]).read_text().splitlines()[0], 'Work')
                    launcher.wait_first_action.assert_not_called()

    def test_stop_for_another_session_and_ack_are_received_during_slow_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            journal = Journal(Path(temp) / 'journal.sqlite')
            incoming = queue.Queue()
            launched, release, stopped = threading.Event(), threading.Event(), threading.Event()
            readers, failures = set(), []
            class Socket:
                def __enter__(self): return self
                def __exit__(self, *_): pass
                def recv(self, timeout=None):
                    readers.add(threading.get_ident())
                    try: value = incoming.get(timeout=2)
                    except queue.Empty: raise TimeoutError()
                    if isinstance(value, BaseException): raise value
                    return json.dumps(value)
                def send(self, raw):
                    frame = json.loads(raw)
                    if frame['type'] == 'offer_ack':
                        incoming.put({'type': 'ack', 'session_id': frame['session_id'], 'epoch': frame['epoch']})
            socket = Socket()
            client = SimpleNamespace(socket=lambda _: socket, request=lambda *a: {})
            bridge = Bridge.__new__(Bridge)
            bridge.client, bridge.journal = client, journal
            bridge.budget_paused_until = bridge.budget_safety_paused_until = 0
            bridge.heartbeat, bridge.reconcile = Mock(), Mock()
            def offer(value, acknowledge):
                acknowledge(value['session_id'], 1)
                launched.set()
                if not release.wait(3): raise AssertionError('launch was never released')
                journal.emit('started', '/api/sessions/slow/started', {'native_id': 'native'})
            bridge.offer = offer
            bridge.command = lambda command: stopped.set()
            def serve():
                try: bridge.serve('machine', [])
                except StopServe: pass
                except BaseException as error: failures.append(error)
                finally: journal.close_thread()
            thread = threading.Thread(target=serve)
            incoming.put({'type': 'session.offer', 'offer': {'session_id': 'slow'}})
            thread.start()
            try:
                self.assertTrue(launched.wait(2), 'socket ACK did not reach launch worker')
                incoming.put({'type': 'session.command', 'command': {'session_id': 'other', 'kind': 'stop'}})
                self.assertTrue(stopped.wait(1), 'stop blocked behind unrelated launch')
                self.assertEqual(len(readers), 1, 'launch worker read the shared socket')
            finally:
                release.set()
                incoming.put(StopServe())
                thread.join(4)
                bridge._launch_pool.shutdown(wait=True)
                bridge._command_pool.shutdown(wait=True)
                self.assertFalse(thread.is_alive())
                self.assertFalse(failures)
                journal.close_thread()


if __name__ == '__main__':
    unittest.main()
