"""Intake 88ff6766: a Windows controller never exits on one failed child.

LAPTOP-PHA6TCNC logged only controller_started + controller_cleanup_unconfirmed
and then stayed dead: the supervisor raised on the exited child, the exited
child refused `aplexer kill`, cleanup was marked unproven, the process exited 1
and Task Scheduler (whose RestartCount fires only on launch failure, never on a
non-zero exit) did not start it again.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_hub_host import bootstrap as b, bootstrap_control as c, private_fs

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts/session-host-supervisor.py'


def load_supervisor():
    spec = importlib.util.spec_from_file_location('supervisor_88ff6766', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def events(text):
    return [json.loads(line) for line in text.splitlines() if line.startswith('{')]


class WindowsControllerRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.config = self.home / 'config.json'
        self.config.write_text('{}')
        self.control = self.home / 'control'

    def run_main(self, supervisor, native, stop):
        argv = [str(SCRIPT), '--workspace', str(self.home), '--config', str(self.config),
                '--credential-config', str(self.config), '--journal-dir', str(self.home / 'journal'),
                '--control-dir', str(self.control)]
        output = io.StringIO()
        with patch.object(supervisor.sys, 'argv', argv), \
             patch.object(supervisor, 'is_windows', return_value=True), \
             patch.object(supervisor.signal, 'signal'), patch.dict(os.environ), \
             patch.object(supervisor.threading, 'Event', return_value=stop), \
             patch.object(supervisor, 'publish_controller_crash', return_value=False), \
             patch.object(supervisor, 'child_output_tail', return_value='Traceback: ValueError: boom'), \
             patch.object(c, 'boot_identity', return_value=None), \
             patch('time.sleep'), \
             patch.object(supervisor, 'native', side_effect=native), \
             contextlib.redirect_stdout(output):
            result = supervisor.main()
        return result, events(output.getvalue())

    def fake_native(self, *, kill_refuses):
        children, calls = [], []

        def native(*args):
            calls.append(args)
            if args[0] == 'start':
                children.append(dict(id='child-%d' % len(children), tag=args[args.index('--tag') + 1],
                                     workspace=str(self.home), engine='shell', created_at_ms=len(children) + 1,
                                     command=list(args[args.index('--') + 1:])))
                return dict(children[-1])
            child = next(r for r in children if r['id'] == args[1])
            if args[0] == 'kill':
                if kill_refuses and child['id'] == 'child-0':
                    raise RuntimeError('Native controller operation failed')
                return {'id': child['id'], 'record_removed': True}
            # The first child dies right away (e.g. a session_host startup error).
            if child['id'] == 'child-0':
                return dict(child, phase='exited', worker_alive=False, containment_empty=True)
            return dict(child, phase='running', worker_alive=True)
        return native, children, calls

    def test_exited_child_is_proven_without_kill_and_a_new_child_starts(self):
        supervisor = load_supervisor()
        native, children, calls = self.fake_native(kill_refuses=True)
        stop = Mock()
        # attempt 1: child-0 dies; retry wait; attempt 2: child-1 runs until stop.
        stop.is_set.side_effect = [False, False, False, True]
        stop.wait.side_effect = [False, False, False, True, True]
        result, log = self.run_main(supervisor, native, stop)
        self.assertIsNone(result)
        self.assertEqual([c['id'] for c in children], ['child-0', 'child-1'])
        names = [e['event'] for e in log]
        self.assertNotIn('controller_cleanup_unconfirmed', [e['event'] for e in log if e.get('id') == 'child-0'])
        failed = next(e for e in log if e['event'] == 'controller_failed')
        self.assertEqual((failed['id'], failed['type']), ('child-0', 'RuntimeError'))
        self.assertIn('exited', failed['detail'])
        self.assertIn('controller_child_output', names)
        retry = next(e for e in log if e['event'] == 'controller_retry')
        self.assertEqual(retry['retry_in_s'], supervisor.RETRY_DELAYS_S[0])
        self.assertFalse([c for c in calls if c[0] == 'kill' and c[1] == 'child-0'],
                         'an exited, contained child needs no kill')
        self.assertFalse((self.control / 'needs-attention.json').exists())

    def test_unproven_cleanup_is_logged_and_retried_never_exits(self):
        supervisor = load_supervisor()
        children, calls = [], []

        def native(*args):
            calls.append(args)
            if args[0] == 'start':
                children.append(dict(id='only', tag=args[args.index('--tag') + 1], workspace=str(self.home),
                                     engine='shell', created_at_ms=1, command=list(args[args.index('--') + 1:])))
                return dict(children[-1])
            if args[0] == 'kill':
                raise RuntimeError('Native controller operation failed')
            raise RuntimeError('Native controller operation failed')  # status unavailable
        stop = Mock()
        stop.is_set.side_effect = [False, False, True]
        stop.wait.return_value = False
        with patch.object(c, 'observe_receipt', return_value=None), \
             patch.object(c, 'receipt_absent', return_value=False):
            result, log = self.run_main(supervisor, native, stop)
        self.assertIsNone(result, 'the supervisor returns only on stop, never exit 1')
        names = [e['event'] for e in log]
        cleanup = next(e for e in log if e['event'] == 'controller_cleanup_unconfirmed')
        self.assertEqual(cleanup['type'], 'RuntimeError')
        self.assertTrue((self.control / 'needs-attention.json').exists())
        retries = [e for e in log if e['event'] == 'controller_retry']
        self.assertEqual([r['type'] for r in retries], ['RuntimeError', 'NeedsAttention'])
        self.assertEqual(len(children), 1, 'unproven exit never starts a duplicate child')
        self.assertIn('controller_failed', names)

    def test_backoff_caps_and_resets_after_a_healthy_run(self):
        supervisor = load_supervisor()
        stop = Mock()
        stop.is_set.side_effect = [False] * 6 + [True]
        stop.wait.return_value = True
        clock = iter([0, 1, 1, 2, 2, 3, 3, 4, 4, 1000, 1000, 1001])
        attempt = Mock(side_effect=RuntimeError('x'))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            supervisor.supervise_windows(attempt, stop, clock=lambda: next(clock))
        stop.wait.return_value = True  # each wait slice ends early; delays come from the log
        delays = [e['retry_in_s'] for e in events(output.getvalue())]
        self.assertEqual(delays, [5, 15, 30, 60, 5, 15])

    def test_planned_exits_propagate(self):
        supervisor = load_supervisor()
        for code in (supervisor.RELEASE_RESTART_EXIT, supervisor.HANDOVER_EXIT):
            stop = Mock()
            stop.is_set.return_value = False
            with self.assertRaises(SystemExit) as exit:
                supervisor.supervise_windows(Mock(side_effect=SystemExit(code)), stop)
            self.assertEqual(exit.exception.code, code)

    def test_redaction_drops_tokenish_values(self):
        supervisor = load_supervisor()
        text = supervisor.redact('Authorization: Bearer abc.def access_token=xyz ' + 'A' * 40)
        self.assertNotIn('abc.def', text)
        self.assertNotIn('xyz', text)
        self.assertNotIn('A' * 40, text)


class ControllerRunnerLoopTests(unittest.TestCase):
    def test_runner_restarts_supervisor_on_failure_and_ends_on_stop_or_handover(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            base = home / 'AppData/Local/Programs/agent-hub'
            release = base / 'releases' / ('0' * 16 + '-' + '1' * 12)
            release.mkdir(parents=True)
            with patch.object(b.sys, 'platform', 'win32'), \
                 patch.object(private_fs, '_windows', return_value=False), \
                 patch.object(b, 'architecture', return_value='x64'), \
                 patch.object(b.Path, 'home', return_value=home), \
                 patch.dict(os.environ, {'LOCALAPPDATA': str(home / 'AppData/Local'),
                                         'APPDATA': str(home / 'AppData/Roaming'), 'SystemRoot': 'C:/Windows'}), \
                 patch.object(c, 'handover'), patch.object(b, 'run') as run:
                self.assertEqual(b.install_service(release, ['py', '-m', 'agent_hub_host.host_supervisor']),
                                 'windows_user_logon_task')
            script = (release / 'controller.ps1').read_text()
        self.assertIn("$ErrorActionPreference = 'Continue'", script)
        loop = script[script.index('while ($true) {'):]
        self.assertIn("& 'py' '-m' 'agent_hub_host.host_supervisor'", loop)
        self.assertIn('if ($code -eq 0 -or $code -eq 76) { exit $code }', loop)
        self.assertIn('for ($i = 0; $i -lt 30; $i++) {', loop)
        self.assertIn("replace-pending' -ErrorAction SilentlyContinue", loop)
        self.assertIn('{ exit 76 }', loop)
        task = run.call_args.args[0][-1]
        self.assertLess(task.index('Remove-Item'), task.index('Start-ScheduledTask'),
                        'the marker is cleared before the new task can see it')
        self.assertNotIn('\nexit $LASTEXITCODE', script)
        self.assertIn('-RestartCount 999', run.call_args.args[0][-1])


class MaxAgentsAutoSizingTests(unittest.TestCase):
    def test_sizing_uses_ram_and_cpu_and_clamps(self):
        from agent_hub_host.host_sizing import size_max_agents
        self.assertEqual(size_max_agents(32 * 1024, 16), 18)    # LAPTOP-class: RAM-bound
        self.assertEqual(size_max_agents(32 * 1024, 4), 8)      # CPU-bound
        self.assertEqual(size_max_agents(4096, 8), 1)           # never below 1
        self.assertEqual(size_max_agents(512 * 1024, 128), 100) # never above 100
        self.assertEqual(size_max_agents(None, None), 1)

    def test_auto_resolves_at_load_and_survives_probe_failure(self):
        from agent_hub_host import fleet, host_sizing
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'subscriptions.json'
            path.write_text(json.dumps({'version': 1, 'subscriptions': [], 'capabilities': [],
                                        'max_agents': 'auto', 'workspaces': {'home': tmp}}))
            settings = fleet.load_settings(path)
            self.assertEqual(settings['max_agents'], 'auto', 'raw value survives write-back')
            with patch.object(host_sizing, 'auto_max_agents', return_value=12):
                self.assertEqual(fleet.resolve_max_agents(settings['max_agents']), 12)
            self.assertEqual(fleet.resolve_max_agents(6), 6)
            self.assertEqual(host_sizing.auto_max_agents(memory_fn=Mock(side_effect=OSError),
                                                         cpu_count_fn=lambda: None), 1)
            self.assertEqual(json.loads(path.read_text())['max_agents'], 'auto', 'file is never rewritten')

    def test_installer_default_migrates_once_and_keeps_explicit_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, settings = Path(tmp) / 'state', Path(tmp) / 'subscriptions.json'
            settings.write_text(json.dumps({'version': 1, 'max_agents': 1, 'subscriptions': []}))
            self.assertTrue(b.migrate_installer_max_agents(settings, state))
            self.assertEqual(json.loads(settings.read_text())['max_agents'], 'auto')
            settings.write_text(json.dumps({'version': 1, 'max_agents': 1}))  # operator re-pins 1
            self.assertFalse(b.migrate_installer_max_agents(settings, state))
            self.assertEqual(json.loads(settings.read_text())['max_agents'], 1)
        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / 'subscriptions.json'
            settings.write_text(json.dumps({'version': 1, 'max_agents': 6}))
            self.assertFalse(b.migrate_installer_max_agents(settings, Path(tmp) / 'state'))
            self.assertEqual(json.loads(settings.read_text())['max_agents'], 6)


class ReviewFindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.control = Path(self.temp.name) / 'control'

    def test_failed_start_records_pending_tag_and_recovers_only_when_tag_absent(self):
        with self.assertRaises(RuntimeError):
            with c.ControllerLease(self.control, boot_probe=lambda: 'b') as lease:
                lease.publish_pending('hub-controller-abc')
                raise RuntimeError('start timed out')
        owner = json.loads((self.control / 'owner.json').read_text())
        self.assertEqual((owner['receipt'], owner['pending_tag']), (None, 'hub-controller-abc'))
        self.assertTrue((self.control / 'needs-attention.json').exists())
        with self.assertRaises(c.NeedsAttention):
            c.recover_stale_owner(self.control, 'b', lambda r: None, None, lambda tag: False)
        self.assertTrue((self.control / 'owner.json').exists(), 'a live tagged row blocks a new child')
        c.recover_stale_owner(self.control, 'b', lambda r: None, None, lambda tag: tag == 'hub-controller-abc')
        self.assertFalse((self.control / 'owner.json').exists())
        self.assertFalse((self.control / 'needs-attention.json').exists())

    def test_supervisor_start_failure_publishes_pending_tag(self):
        supervisor = load_supervisor()
        home = Path(self.temp.name).resolve()
        config = home / 'config.json'
        config.write_text('{}')
        argv = [str(SCRIPT), '--workspace', str(home), '--config', str(config),
                '--credential-config', str(config), '--journal-dir', str(home / 'journal'),
                '--control-dir', str(self.control)]
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        stop.wait.return_value = True
        with patch.object(supervisor.sys, 'argv', argv), \
             patch.object(supervisor, 'is_windows', return_value=True), \
             patch.object(supervisor.signal, 'signal'), patch.dict(os.environ), \
             patch.object(supervisor.threading, 'Event', return_value=stop), \
             patch.object(c, 'boot_identity', return_value=None), \
             patch.object(supervisor, 'native', side_effect=RuntimeError('Native controller operation failed')), \
             contextlib.redirect_stdout(io.StringIO()):
            supervisor.main()
        owner = json.loads((self.control / 'owner.json').read_text())
        self.assertTrue(owner['pending_tag'].startswith('hub-controller-'))

    def test_replace_marker_ends_retry_loop_before_and_between_attempts(self):
        supervisor = load_supervisor()
        stop = Mock()
        stop.is_set.return_value = False
        with self.assertRaises(SystemExit) as exit, contextlib.redirect_stdout(io.StringIO()):
            supervisor.supervise_windows(Mock(), stop, replacing=lambda: True)
        self.assertEqual(exit.exception.code, supervisor.HANDOVER_EXIT)
        stop.wait.return_value = False
        flags = iter([False, False, True, True])
        attempt = Mock(side_effect=RuntimeError('x'))
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            supervisor.supervise_windows(attempt, stop, replacing=lambda: next(flags))
        self.assertEqual(attempt.call_count, 1)
        self.assertEqual(stop.wait.call_count, 2, 'the 5s wait is cut short by the marker')

    def test_replace_marker_freshness(self):
        c.request_replace(self.control)
        self.assertTrue(c.replace_pending(self.control))
        import time as _t
        self.assertFalse(c.replace_pending(self.control, now=_t.time() + 200))
        c.clear_replace(self.control)
        self.assertFalse(c.replace_pending(self.control))
        c.clear_replace(self.control)

    def test_failed_handover_clears_replace_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            base = home / 'AppData/Local/Programs/agent-hub'
            release = base / 'releases' / ('0' * 16 + '-' + '1' * 12)
            release.mkdir(parents=True)
            seen = []
            def handover(directory):
                seen.append((directory / c.REPLACE_MARKER).exists())
                raise RuntimeError('unconfirmed')
            with patch.object(b.sys, 'platform', 'win32'), \
                 patch.object(private_fs, '_windows', return_value=False), \
                 patch.object(b, 'architecture', return_value='x64'), \
                 patch.object(b.Path, 'home', return_value=home), \
                 patch.dict(os.environ, {'LOCALAPPDATA': str(home / 'AppData/Local'),
                                         'APPDATA': str(home / 'AppData/Roaming'), 'SystemRoot': 'C:/Windows'}), \
                 patch.object(c, 'handover', side_effect=handover), patch.object(b, 'run') as run:
                with self.assertRaises(RuntimeError):
                    b.install_service(release, ['py'])
            self.assertEqual(seen, [True])
            self.assertFalse((base / 'controller' / c.REPLACE_MARKER).exists())
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
