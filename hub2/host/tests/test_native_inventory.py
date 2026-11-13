"""Native all-registry census validation with captured API shapes; no launches."""
import json
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from hub2_agent_hub_host.native_inventory import collect_native_inventory
from hub2_agent_hub_host.native_launch import NativeLauncher


class NativeInventoryTests(unittest.TestCase):
    def native_row(self):
        fixture = Path(__file__).resolve().parents[2] / 'tests/e2e/fixtures/aplexer.json'
        row = json.loads(fixture.read_text())['status_running']
        return {**row, 'id': '11111111-1111-4111-8111-111111111111',
                'workspace': str(Path.cwd()), 'cwd': str(Path.cwd()), 'tag': 'task-hub2-known', 'schema_version': 1,
                'env': {'SECRET': 'must-not-survive'}, 'command': ['private-prompt']}

    def query(self, rows, returncode=0):
        return lambda: subprocess.CompletedProcess(['a'], returncode, json.dumps(rows), '')

    def test_actual_native_entry_requests_full_array_and_excludes_private_data(self):
        row = self.native_row()
        calls = []
        def runner(args, **options):
            calls.append((args, options))
            return subprocess.CompletedProcess(args, 0, json.dumps([row]), '')
        launcher = NativeLauncher({'private_provider_homes': True}, runner, aplexer=sys.executable)
        inventory = launcher.inventory()
        self.assertEqual(calls[0][0], [sys.executable, 'list', '--all', '--json'])
        self.assertEqual(calls[0][1]['timeout'], 5)
        self.assertIsNone(calls[0][1]['cwd'])
        self.assertEqual(len(inventory.identities), 1)
        self.assertNotIn('SECRET', str(inventory))
        self.assertNotIn('private-prompt', str(inventory))
        self.assertIsNone(inventory.absence_evidence(row['workspace'], row['tag']))
        evidence = inventory.absence_evidence(row['workspace'], 'task-hub2-missing')
        self.assertTrue(evidence['complete'])
        self.assertEqual(evidence['row_count'], 1)
        self.assertEqual(evidence['dispatch_provenance'], 'legacy_unknown')
        self.assertNotIn('native_exit_confirmed', evidence)

    def test_bad_envelopes_rows_and_safety_bounds_never_produce_absence(self):
        row = self.native_row()
        cases = [None, {'sessions': []}, [row, row], [dict(row, id='prefix')],
                 [dict(row, workspace='relative')], [dict(row, workspace='/bad\0path')],
                 [dict(row, schema_version=True)]]
        for value in cases:
            with self.subTest(value=str(value)[:80]), self.assertRaisesRegex(RuntimeError, 'native_census_unavailable'):
                collect_native_inventory(self.query(value))
        with self.assertRaisesRegex(RuntimeError, 'native_census_unavailable'):
            collect_native_inventory(self.query([], 1))
        with patch('hub2_agent_hub_host.native_inventory.MAX_CENSUS_ROWS', 0), self.assertRaises(RuntimeError):
            collect_native_inventory(self.query([row]))
        with patch('hub2_agent_hub_host.native_inventory.MAX_CENSUS_BYTES', 1), self.assertRaises(RuntimeError):
            collect_native_inventory(self.query([]))

    def test_deleted_workspace_matches_and_a_stale_snapshot_cannot_close(self):
        workspace = str(Path.cwd() / 'already-deleted-workspace')
        row = {**self.native_row(), 'workspace': workspace + ' (deleted)'}
        inventory = collect_native_inventory(self.query([row]))
        self.assertIsNone(inventory.absence_evidence(workspace, row['tag']))
        with patch('hub2_agent_hub_host.native_inventory.time.monotonic', return_value=inventory.observed_monotonic+6):
            with self.assertRaisesRegex(RuntimeError, 'native_census_stale'):
                inventory.absence_evidence(workspace, 'task-hub2-missing')


    def test_full_publication_chunks_are_bounded_sanitized_and_digest_exact(self):
        from hub2_agent_hub_host.client import Client
        import hashlib
        rows = [{**self.native_row(), 'id': '11111111-1111-4111-8111-%012d' % i,
                 'workspace': '/work/' + 'é' * 500, 'cwd': '/work/peer',
                 'engine': 'shell', 'agent': 'codex', 'agent_profile': 'default',
                 'phase': 'running', 'last_activity_ms': None} for i in range(40)]
        inventory = collect_native_inventory(self.query(rows))
        client = Client('https://hub.test', 'synthetic')
        packets = []
        with patch.object(client, 'request', side_effect=lambda method, path, body, **kw: packets.append((path, body, kw))):
            client.publish_native_inventory('host-1', inventory)
        self.assertGreater(len(packets), 1)
        for path, packet, kwargs in packets:
            self.assertEqual(path, '/api/machines/host-1/native-inventory')
            self.assertLess(len(json.dumps(packet).encode()), 65536)
            self.assertLessEqual(kwargs['timeout'], 5)
        raw = ''.join(packet['data'] for _, packet, _ in packets)
        self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(), packets[0][1]['sha256'])
        published = json.loads(raw)
        self.assertEqual(len(published), 40)
        self.assertEqual(published[0]['workspace'], rows[0]['workspace'])
        self.assertIsNone(published[0]['last_activity_ms'])
        self.assertEqual(published[0]['agent'], 'codex')
        for secret in ('SECRET', 'private-prompt', 'socket_path', 'history_path', 'env'):
            self.assertNotIn(secret, raw)

    def test_derived_native_state_is_preserved_separately_from_persisted_phase(self):
        raw = {**self.native_row(), 'phase': 'running', 'state': 'broken', 'worker_alive': False}
        rows = collect_native_inventory(self.query([raw])).rows
        self.assertEqual(rows[0]['phase'], 'running')
        self.assertEqual(rows[0]['state'], 'broken')
        self.assertIs(rows[0]['worker_alive'], False)
        unknown = {key: value for key, value in raw.items() if key not in ('state', 'worker_alive')}
        rows = collect_native_inventory(self.query([unknown])).rows
        self.assertEqual(rows[0]['state'], 'unknown')
        self.assertIsNone(rows[0]['worker_alive'])
        for invalid in ({**raw, 'worker_alive': 'false'}, {**raw, 'state': 'inferred-live'}):
            with self.assertRaises(RuntimeError):
                collect_native_inventory(self.query([invalid]))

    def test_invalid_observed_fields_never_publish_guessed_or_truncated_inventory(self):
        row = self.native_row()
        for changes in ({'engine': '/private/token'}, {'profile': 'x'*129},
                        {'last_activity_ms': True}, {'cwd': 'relative'}, {'phase': 'assumed-dead'}):
            with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                collect_native_inventory(self.query([{**row, **changes}]))
        from dataclasses import replace
        inventory = collect_native_inventory(self.query([row]))
        with self.assertRaisesRegex(RuntimeError, 'native_census_incomplete'):
            replace(inventory, rows=()).upload_chunks()
        empty = collect_native_inventory(self.query([])).upload_chunks()
        self.assertEqual(empty[0]['row_count'], 0)
        self.assertEqual(empty[0]['data'], '[]')

    def test_publication_unavailable_does_not_abort_managed_heartbeat(self):
        from hub2_agent_hub_host.agent import Bridge
        from types import SimpleNamespace
        from unittest.mock import Mock
        bridge = Bridge.__new__(Bridge)
        bridge.engines = {'codex'}
        bridge.machine_id = 'h1'
        bridge.launcher = SimpleNamespace(capabilities=lambda: {'codex': {'headless'}},
                                          inventory=Mock(side_effect=RuntimeError('secret-do-not-log')))
        bridge.client = SimpleNamespace(heartbeat=Mock(), publish_native_inventory=Mock())
        bridge.live_sessions = lambda: []
        with self.assertLogs('hub2_agent_hub_host.agent', level='WARNING') as logs:
            bridge.heartbeat([])
            bridge._native_inventory_thread.join(timeout=1)
        bridge.client.heartbeat.assert_called_once()
        bridge.client.publish_native_inventory.assert_not_called()
        self.assertNotIn('secret-do-not-log', str(logs.output))
        bridge.launcher.inventory = lambda: collect_native_inventory(self.query([]))
        bridge.client.publish_native_inventory.side_effect = RuntimeError('upload refused 404/503')
        with self.assertLogs('hub2_agent_hub_host.agent', level='WARNING'):
            bridge.heartbeat([])
            bridge._native_inventory_thread.join(timeout=1)
        self.assertEqual(bridge.client.heartbeat.call_count, 2)

    def test_slow_census_never_delays_heartbeats_and_only_one_worker_runs(self):
        from hub2_agent_hub_host.agent import Bridge
        from types import SimpleNamespace
        from unittest.mock import Mock
        import threading
        entered, release = threading.Event(), threading.Event()
        inventory = collect_native_inventory(self.query([]))
        def collect():
            entered.set()
            release.wait(2)
            return inventory
        bridge = Bridge.__new__(Bridge)
        bridge.engines, bridge.machine_id = {'codex'}, 'h1'
        bridge.launcher = SimpleNamespace(capabilities=lambda: {'codex': {'headless'}}, inventory=Mock(side_effect=collect))
        bridge.client = SimpleNamespace(heartbeat=Mock(), publish_native_inventory=Mock())
        bridge.live_sessions = lambda: []
        try:
            bridge.heartbeat([])
            self.assertTrue(entered.wait(1))
            worker = bridge._native_inventory_thread
            bridge.heartbeat([])
            self.assertIs(worker, bridge._native_inventory_thread)
            self.assertEqual(bridge.client.heartbeat.call_count, 2)
            self.assertEqual(bridge.launcher.inventory.call_count, 1)
        finally:
            release.set()
            bridge._native_inventory_thread.join(timeout=1)
        bridge.client.publish_native_inventory.assert_called_once()

    def test_blocked_observation_does_not_delay_real_serve_connect_or_stop_dispatch(self):
        from hub2_agent_hub_host.agent import Bridge, Journal
        from types import SimpleNamespace
        from unittest.mock import Mock
        import tempfile
        import threading
        class Finished(BaseException):
            pass
        for blocked in ('collection', 'upload'):
            with self.subTest(blocked=blocked), tempfile.TemporaryDirectory() as temp:
                entered, release = threading.Event(), threading.Event()
                inventory = collect_native_inventory(self.query([]))
                def collect():
                    if blocked == 'collection':
                        entered.set()
                        release.wait(2)
                    return inventory
                def publish(*args, **kwargs):
                    if blocked == 'upload':
                        entered.set()
                        release.wait(2)
                command = {'id': 'stop-once', 'session_id': 'owned', 'kind': 'stop', 'epoch': 1}
                class Socket:
                    calls = 0
                    def __enter__(self):
                        return self
                    def __exit__(self, *_):
                        return False
                    def recv(socket, **kwargs):
                        socket.calls += 1
                        if socket.calls == 1:
                            self.assertTrue(entered.wait(1))
                            return json.dumps({'type': 'session.command', 'command': command})
                        raise Finished()
                client = SimpleNamespace(heartbeat=Mock(), publish_native_inventory=publish,
                                         socket=Mock(return_value=Socket()), request=Mock(return_value={}))
                journal = Journal(Path(temp) / 'journal.sqlite')
                bridge = Bridge(client, journal, [temp], ['codex'], executable=sys.executable)
                bridge.launcher = SimpleNamespace(capabilities=lambda: {'codex': {'headless'}}, inventory=collect)
                bridge.command = Mock()
                try:
                    with self.assertRaises(Finished):
                        bridge.serve('h1', [])
                    client.socket.assert_called_once_with('h1')
                    bridge.command.assert_called_once_with(command)
                    self.assertTrue(bridge._native_inventory_stop.is_set())
                finally:
                    release.set()
                    bridge._native_inventory_thread.join(timeout=1)
                    journal.close_thread()

    def test_late_collection_after_shutdown_cannot_publish(self):
        from hub2_agent_hub_host.agent import Bridge
        from types import SimpleNamespace
        from unittest.mock import Mock
        import threading
        entered, release = threading.Event(), threading.Event()
        inventory = collect_native_inventory(self.query([]))
        def collect():
            entered.set()
            release.wait(2)
            return inventory
        bridge = Bridge.__new__(Bridge)
        bridge.engines, bridge.machine_id = {'codex'}, 'h1'
        bridge.launcher = SimpleNamespace(capabilities=lambda: {'codex': {'headless'}}, inventory=collect)
        bridge.client = SimpleNamespace(heartbeat=Mock(), publish_native_inventory=Mock())
        bridge.live_sessions = lambda: []
        try:
            bridge.heartbeat([])
            self.assertTrue(entered.wait(1))
            bridge._native_inventory_stop.set()
        finally:
            release.set()
            bridge._native_inventory_thread.join(timeout=1)
        bridge.heartbeat([])
        bridge.client.publish_native_inventory.assert_not_called()
        self.assertEqual(bridge.client.heartbeat.call_count, 2)

    def test_cancelled_upload_does_not_send_remaining_chunks(self):
        from hub2_agent_hub_host.client import Client
        import threading
        stop = threading.Event()
        rows = [{**self.native_row(), 'id': '11111111-1111-4111-8111-%012d' % i,
                 'workspace': '/work/' + 'x'*1000} for i in range(40)]
        inventory = collect_native_inventory(self.query(rows))
        client = Client('https://hub.test')
        def cancel(*args, **kwargs):
            stop.set()
        with patch.object(client, 'request', side_effect=cancel) as request:
            client.publish_native_inventory('h1', inventory, stop_event=stop)
        self.assertEqual(request.call_count, 1)

    def test_publication_stops_on_first_error_and_deadline_without_retrying_forever(self):
        from hub2_agent_hub_host.client import Client, ApiError
        inventory = collect_native_inventory(self.query([self.native_row()]))
        client = Client('https://hub.test')
        with patch.object(client, 'request', side_effect=ApiError('unavailable')) as request:
            with self.assertRaises(ApiError):
                client.publish_native_inventory('h1', inventory)
            self.assertEqual(request.call_count, 1)
        with patch('hub2_agent_hub_host.client.time.monotonic', side_effect=[0, 30]), patch('hub2_agent_hub_host.native_inventory.NativeInventory.fresh', return_value=True), patch.object(client, 'request') as request:
            with self.assertRaisesRegex(ApiError, 'deadline'):
                client.publish_native_inventory('h1', inventory)
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
