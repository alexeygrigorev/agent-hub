"""Root producer control flow with fake descriptors and a failing fake exec.

Never creates a watch/socket, changes UID, executes a Host, or qualifies runtime.
"""
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


SPEC = importlib.util.spec_from_file_location('natural_prepare',
    Path(__file__).resolve().parents[3] / 'scripts/hub-alert-quota-prepare.py')
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


class Tests(unittest.TestCase):
    def run_fake(self, *, source_failure=False, replaced=False, short_write=False,
                 gate_close_failure=False):
        args = SimpleNamespace(target_uid=1000, python_bin='/usr/bin/python3.12',
            entry_archive='/owned/host.pyz', source_dir='/owned/source',
            selection='/owned/selection.json', config='/user/config.json',
            broker_dir='/user/broker', gate_dir='/user/gate', host_aplexer='/user/pinned/a')
        events, written = [], []
        failure = OSError('fake exec failed')
        gate = Mock()
        gate.fileno.return_value = 20
        if gate_close_failure:
            gate.close.side_effect = OSError('fake gate close failed')
        def write(fd, raw):
            self.assertEqual(fd, 72)
            written.append(json.loads(raw))
            events.append('write')
            return len(raw) - int(short_write)
        def drop(uid, gid):
            self.assertEqual((uid, gid), (1000, 1001))
            events.append('drop')
        def execute(path, command, env):
            events.append(('exec', path, command, env))
            self.assertEqual(events[-2], 'drop')
            raise failure
        source_results = [(13, 14, {'name': 'hub-quota-publish.py'}),
            (15, 16, {'name': 'hub_alert_client.py'}),
            (17, 18, {'name': 'selection.json'})]
        if source_failure:
            source_results[1] = PermissionError('fake source refused')
        with ExitStack() as stack:
            stack.enter_context(patch.object(prepare.os, 'getuid', return_value=0))
            stack.enter_context(patch.object(prepare.os, 'geteuid', return_value=0))
            stack.enter_context(patch.object(prepare.pwd, 'getpwuid', return_value=SimpleNamespace(
                pw_uid=1000, pw_gid=1001, pw_dir='/user')))
            stack.enter_context(patch.object(prepare, 'pin_interpreter', return_value=(11, {'path': args.python_bin})))
            stack.enter_context(patch.object(prepare, 'pin_entry_archive', return_value=(12, {'path': args.entry_archive})))
            stack.enter_context(patch.object(prepare, '_pin_natural_source', side_effect=source_results))
            stack.enter_context(patch.object(prepare, 'open_fanotify_group', return_value=19))
            stack.enter_context(patch.object(prepare, 'bind_source_gate', return_value=(gate, 21, 22, (7, 8))))
            stack.enter_context(patch.object(prepare, 'validate_user_runtime', return_value={'XDG_RUNTIME_DIR': '/run/user/1000'}))
            stack.enter_context(patch.object(prepare.os, 'pipe2', return_value=(71, 72)))
            stack.enter_context(patch.object(prepare.os, 'write', side_effect=write))
            inherited = stack.enter_context(patch.object(prepare.os, 'set_inheritable'))
            closed = stack.enter_context(patch.object(prepare.os, 'close'))
            unlink = stack.enter_context(patch.object(prepare.os, 'unlink'))
            stack.enter_context(patch.object(prepare.os, 'stat', return_value=SimpleNamespace(st_dev=7, st_ino=9 if replaced else 8)))
            dropped = stack.enter_context(patch.object(prepare, 'drop_privileges', side_effect=drop))
            # An accidental subprocess creates an immediate test failure.
            stack.enter_context(patch.object(prepare.subprocess, 'run', side_effect=AssertionError('subprocess forbidden')))
            with self.assertRaises((OSError, PermissionError)) as caught:
                prepare.prepare_natural_and_exec(args, execve=execute)
            return SimpleNamespace(events=events, written=written, gate=gate,
                failure=failure, caught=caught.exception, closed=closed.call_args_list,
                inherited=inherited.call_args_list, unlinks=unlink.call_count,
                drops=dropped.call_count)

    def test_root_pipe_descriptors_are_donated_before_drop_and_single_process_exec(self):
        r = self.run_fake()
        self.assertIs(r.caught, r.failure)
        self.assertEqual(r.events[:2], ['write', 'drop'])
        _, path, command, env = r.events[2]
        self.assertEqual(path, '/proc/self/fd/11')
        self.assertEqual(command, ['/proc/self/fd/11', '-I', '/proc/self/fd/12',
            'natural-host', '--config', '/user/config.json', 'serve', '--aplexer',
            '/user/pinned/a', '--alert-handoff-fd', '71'])
        self.assertNotIn('PYTHONPATH', env)
        self.assertEqual(env['HOME'], '/user')
        manifest = r.written[0]
        self.assertEqual(manifest['kind'], 'natural_quota_root_donation_v1')
        self.assertEqual(manifest['v'], 2)
        self.assertIsNone(manifest['ack_fd'])
        self.assertEqual(manifest['source']['fd'], 13)
        self.assertEqual({call.args[0] for call in r.inherited}, set(range(11, 23)) | {71})
        self.assertEqual(len(r.closed), len({call.args[0] for call in r.closed}))

    def test_partial_open_failure_closes_every_acquired_descriptor_before_drop(self):
        r = self.run_fake(source_failure=True)
        self.assertEqual({call.args[0] for call in r.closed}, {11, 12, 13, 14})
        self.assertEqual(r.drops, 0)
        self.assertEqual(r.events, [])

    def test_short_manifest_write_never_drops_or_executes_and_cleans_up(self):
        r = self.run_fake(short_write=True)
        self.assertEqual(r.drops, 0)
        self.assertEqual(r.events, ['write'])
        self.assertEqual({call.args[0] for call in r.closed}, set(range(11, 20)) | {21, 22, 71, 72})

    def test_foreign_replacement_node_is_not_unlinked_after_exec_failure(self):
        r = self.run_fake(replaced=True)
        self.assertEqual(r.unlinks, 0)
        r.gate.close.assert_called_once_with()
        self.assertEqual(len(r.closed), 13)

    def test_gate_close_failure_preserves_exec_failure_and_closes_remaining_fds(self):
        r = self.run_fake(gate_close_failure=True)
        self.assertIs(r.caught, r.failure)
        self.assertEqual(len(r.closed), 13)

    def test_non_root_entry_refuses_before_touching_privileged_resources(self):
        with patch.object(prepare.os, 'getuid', return_value=1000), \
             patch.object(prepare, 'pin_interpreter') as pin:
            with self.assertRaises(PermissionError):
                prepare.prepare_natural_and_exec(SimpleNamespace())
            pin.assert_not_called()


if __name__ == '__main__':
    unittest.main()
