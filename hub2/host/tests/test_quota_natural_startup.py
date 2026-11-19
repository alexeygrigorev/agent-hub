"""Donation ownership boundaries using fake OS metadata; no privileged watches.

These tests do not qualify a live Root handoff or aggregate alert coverage.
"""
import io
import json
import os
import stat
import sys
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_hub_host import alert_authority as ram
from agent_hub_host import quota_natural_startup as startup


def donation():
    value = object.__new__(startup.NaturalDonation)
    value.closed = value.started = False
    value.close_error = None
    value.manager = value.broker = value.gate = None
    value.fds = [51, 52]
    value.selection = object()
    value.group = 53
    value.broker_directory = '/owned/broker'
    value.child_interpreter = sys.executable
    return value


class PipeTests(unittest.TestCase):
    def setUp(self):
        self.value = dict.fromkeys(startup.FIELDS)
        self.value.update(v=2, kind='natural_quota_root_donation_v1',
                          target_uid=1000, handoff_deadline_ns=500)

    def read(self, raw=None, *, mode=stat.S_IFIFO, uid=0, flags=os.O_RDONLY):
        data = json.dumps(self.value).encode() if raw is None else raw
        with patch.object(startup.os, 'fstat', return_value=SimpleNamespace(st_mode=mode, st_uid=uid)), \
             patch.object(startup.fcntl, 'fcntl', return_value=flags), \
             patch.object(startup.os, 'set_blocking'), \
             patch.object(startup.select, 'select', return_value=([31], [], [])), \
             patch.object(startup.os, 'read', side_effect=[data, b'']), \
             patch.object(startup.os, 'getuid', return_value=1000), \
             patch.object(startup.time, 'monotonic', return_value=1), \
             patch.object(startup.time, 'monotonic_ns', return_value=100):
            return startup._read_root_pipe(31)

    def test_unrelated_descriptors_are_rejected_without_closing_them(self):
        for options in ({'mode': stat.S_IFREG}, {'uid': 1000}, {'flags': os.O_RDWR}):
            with self.subTest(options=options), patch.object(startup.os, 'close') as close:
                with self.assertRaises(ram.Unknown):
                    self.read(**options)
                close.assert_not_called()

    def test_valid_root_pipe_consumed_once(self):
        with patch.object(startup.os, 'close') as close:
            self.assertEqual(self.read(), self.value)
            close.assert_called_once_with(31)

    def test_closed_donated_fd_cannot_leak_other_authenticated_handles(self):
        value = dict.fromkeys(startup.FIELDS)
        value.update(interpreter={'fd': 11}, entry={'fd': 12},
            selection={'fd': 13, 'dir_fd': 14}, source={'fd': 15, 'dir_fd': 16},
            client={'fd': 17, 'dir_fd': 18}, fan_group=19,
            gate_sock_fd=20, gate_dir_fd=21, gate_node_fd=22, ack_fd=None)
        def fstat(fd):
            if fd == 13:
                raise OSError('closed donated FD')
            return SimpleNamespace()
        with patch.object(startup, '_read_root_pipe', return_value=value), \
             patch.object(startup.os, 'fstat', side_effect=fstat), \
             patch.object(startup.os, 'close') as close, \
             patch.object(startup, '_root_graph') as graph:
            with self.assertRaises(OSError):
                startup.consume_natural_handoff(31)
            graph.assert_not_called()  # no new FD may reuse the missing number
            self.assertEqual([call.args[0] for call in close.call_args_list],
                             list(reversed(range(11, 23))))

    def test_malformed_or_stale_payload_still_closes_consumed_pipe(self):
        for change in ({'target_uid': 1001}, {'handoff_deadline_ns': 100}, {'extra': True}):
            with self.subTest(change=change), patch.object(startup.os, 'close') as close:
                self.value.update(change)
                with self.assertRaises(ram.Unknown):
                    self.read()
                close.assert_called_once_with(31)
                self.setUp()

    def test_duplicate_json_keys_cannot_select_another_uid(self):
        raw = json.dumps(self.value).encode()[:-1] + b',"target_uid":1000}'
        with patch.object(startup.os, 'close') as close:
            with self.assertRaises((ram.Unknown, ValueError)):
                self.read(raw)
            close.assert_called_once_with(31)


class WatchTests(unittest.TestCase):
    def check(self, raw):
        source = SimpleNamespace(pin=SimpleNamespace(dev=7, ino=8))
        with patch.object(startup.m, '_verify_fanotify_fd') as verify, \
             patch('builtins.open', return_value=io.StringIO(raw)):
            startup._notification_only_group(41, source)
            verify.assert_called_once_with(41, 7, 8)

    def test_only_the_single_inode_notification_open_mark_is_accepted(self):
        self.check('pos:\t0\nfanotify flags:0 event-flags:2\n'
                   'fanotify ino:8 sdev:7 mflags:0 mask:20 ignored_mask:0\n')

    def test_permission_groups_and_broader_or_duplicate_marks_are_rejected(self):
        for raw in (
            'fanotify flags:4 event-flags:2\nfanotify ino:8 mask:20\n',
            'fanotify flags:0 event-flags:2\nfanotify ino:8 mask:10020\n',
            'fanotify flags:0 event-flags:2\nfanotify ino:8 mask:20\nfanotify ino:9 mask:20\n',
            'fanotify flags:0 event-flags:2\nfanotify ino:8 mask:20\nfanotify mnt_id:1 mask:20\n',
        ):
            with self.subTest(raw=raw), self.assertRaises(ram.Unknown):
                self.check(raw)


class LifetimeTests(unittest.TestCase):
    def test_cleanup_attempts_every_owned_fd_once_and_retains_failure(self):
        value = donation()
        failure = OSError('close failed')
        with patch.object(startup.os, 'close', side_effect=[failure, None]) as close:
            with self.assertRaises(OSError) as first:
                value.close()
            self.assertIs(first.exception, failure)
            self.assertEqual([call.args[0] for call in close.call_args_list], [52, 51])
            with self.assertRaises(OSError) as second:
                value.close()
            self.assertIs(second.exception, failure)
            self.assertEqual(close.call_count, 2)

    def test_owner_exit_defers_borrowed_descriptor_cleanup_to_manager(self):
        value = donation()
        value.manager = Mock()
        with patch.object(startup.os, 'close') as close:
            value._owner_exit()
            value.manager.close.assert_called_once_with()
            close.assert_not_called()
            self.assertFalse(value.closed)

    def test_factory_failure_before_manager_registration_closes_donation(self):
        value = donation()
        failure = RuntimeError('factory')
        with patch('agent_hub_host.alert_broker.Broker._selected_natural_quota', side_effect=failure), \
             patch.object(startup.os, 'close') as close:
            with self.assertRaises(RuntimeError) as caught:
                value.start()
            self.assertIs(caught.exception, failure)
            self.assertTrue(value.closed)
            self.assertEqual(close.call_count, 2)

    def test_registered_manager_owns_cleanup_when_broker_start_fails(self):
        value = donation()
        manager = Mock()
        failure = RuntimeError('start')
        broker = SimpleNamespace(start=Mock(side_effect=failure))
        def factory(*args, **kwargs):
            kwargs['on_manager'](manager)
            return broker
        with patch('agent_hub_host.alert_broker.Broker._selected_natural_quota', side_effect=factory), \
             patch.object(startup.os, 'close') as close:
            with self.assertRaises(RuntimeError) as caught:
                value.start()
            self.assertIs(caught.exception, failure)
            manager.close.assert_called_once_with()
            close.assert_not_called()

    def test_replaced_gate_node_is_never_unlinked_and_all_handles_retire(self):
        gate = SimpleNamespace(node_identity=(1, 2), listener=Mock(), node_fd=61, dir_fd=62)
        listener = gate.listener
        with patch.object(startup.os, 'stat', return_value=SimpleNamespace(st_dev=1, st_ino=3)), \
             patch.object(startup.os, 'unlink') as unlink, \
             patch.object(startup.os, 'close') as close:
            with self.assertRaises(ram.Unknown):
                startup._close_gate(gate)
            unlink.assert_not_called()
            listener.close.assert_called_once_with()
            self.assertEqual([call.args[0] for call in close.call_args_list], [61, 62])
            startup._close_gate(gate)
            self.assertEqual(close.call_count, 2)


class HostStartupTests(unittest.TestCase):
    def invoke(self, *, failure=None, once=False, bridge_failure=False):
        from agent_hub_host import cli
        value = donation()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps({'machine_id': 'synthetic-machine',
                'roots': [directory], 'engines': [], 'alert_observer': {'configured': True}}))
            args = ['--config', str(config), 'serve', '--alert-handoff-fd', '31']
            if once:
                args.append('--once')
            with patch.object(startup, 'consume_natural_handoff',
                              side_effect=failure, return_value=value) as consume, \
                 patch.object(cli, 'Client'), patch.object(cli, 'Journal'), \
                 patch.object(cli, 'Bridge', side_effect=OSError('bridge') if bridge_failure else None) as bridge, \
                 patch('agent_hub_host.windows_host.configure_logging'), \
                 patch('agent_hub_host.windows_host.host_release', return_value=('test', 1)), \
                 patch('agent_hub_host.alert_broker.configured') as configured, \
                 patch.object(startup, 'os', SimpleNamespace(close=Mock())) as donated_os, \
                 patch('sys.stderr', new_callable=io.StringIO):
                code = cli.main(args)
                consume.assert_called_once_with(31)
                configured.assert_not_called()  # never start two competing broker paths
                calls = bridge.call_args
                heartbeat, serve = bridge.return_value.heartbeat, bridge.return_value.serve
                return code, value, calls, donated_os.close.call_count, heartbeat.call_count, serve.call_count

    def test_real_cli_passes_verified_donation_to_existing_bridge_slot(self):
        code, value, calls, closes, _, serves = self.invoke()
        self.assertEqual(code, 0)
        self.assertIs(calls.kwargs['alert_observer'], value)
        self.assertEqual(serves, 1)
        # The mocked Bridge did not adopt it, so the CLI must close it.
        self.assertTrue(value.closed)
        self.assertEqual(closes, 2)

    def test_unverified_handoff_still_runs_primary_host_without_other_observer(self):
        code, _, calls, closes, _, serves = self.invoke(failure=ram.Unknown('reject'))
        self.assertEqual(code, 0)
        self.assertIsNone(calls.kwargs['alert_observer'])
        self.assertEqual((closes, serves), (0, 1))

    def test_once_closes_unused_donation_after_ordinary_host_work(self):
        code, value, _, closes, beats, serves = self.invoke(once=True)
        self.assertEqual((code, closes, beats, serves), (0, 2, 1, 0))
        self.assertTrue(value.closed)

    def test_bridge_constructor_failure_closes_donation(self):
        code, value, _, closes, _, _ = self.invoke(bridge_failure=True)
        self.assertEqual((code, closes), (1, 2))
        self.assertTrue(value.closed)

    def test_existing_optional_start_consumes_opaque_donation_once(self):
        from agent_hub_host import alert_broker
        value = donation()
        broker = Mock()
        with patch.object(startup.NaturalDonation, 'start', return_value=broker) as start:
            self.assertIs(alert_broker.start_optional(value), broker)
            start.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
