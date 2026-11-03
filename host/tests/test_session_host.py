import importlib.util
import pathlib
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace
from unittest.mock import patch
from agent_hub_host import session_host

class SessionHostTests(unittest.TestCase):
    def setUp(self):
        self._thread_patches = [
            patch.object(session_host, 'maybe_start_status_thread', return_value=None),
            patch.object(session_host, 'maybe_start_metrics_thread', return_value=None),
            patch.object(session_host, 'maybe_start_disk_thread', return_value=None),
            patch.object(session_host, 'maybe_start_host_health_thread', return_value=None),
            patch.object(session_host, 'maybe_start_role_consumer_thread', return_value=None),
        ]
        for p in self._thread_patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._thread_patches):
            p.stop()

    def test_fleet_failure_does_not_block_commands(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(session_host.config,'load_config',return_value={'machine_id':'test-machine','access_token':'synthetic'}), patch.object(session_host.fleet,'load_settings',return_value={}), patch.object(session_host,'qualify_sender_binding',return_value=SimpleNamespace(session_id='test-native',workspace=str(pathlib.Path.cwd()))), patch.object(session_host.fleet,'Dispatcher') as dispatcher, patch.object(session_host.session_control,'execute') as execute:
            dispatcher.return_value.once.side_effect=ValueError('unavailable quota')
            session_host.run('synthetic-settings',directory,cycles=1)
            self.assertEqual(execute.call_count,1)
            args=execute.call_args.args[0]
            self.assertEqual(args.machine_id,'test-machine')
            self.assertEqual(args.sessions_cmd,'host-receive')
            self.assertEqual(args.command_kind,'non_stop')

    def test_urgent_stop_loop_uses_a_stop_only_receiver(self):
        import threading
        stop = threading.Event()
        dispatcher = SimpleNamespace(settings={'aplexer_bin': 'aplexer'})
        calls = []

        def execute(args):
            calls.append(args)
            stop.set()

        with patch.object(session_host.session_control, 'execute', side_effect=execute):
            session_host.urgent_stop_loop(dispatcher, stop, 'test-machine', '/journal', '/repo', poll_s=0)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].command_kind, 'stop')
        self.assertEqual(calls[0].machine_id, 'test-machine')

    def test_heavy_test_reaper_posts_a_sanitized_intake_note(self):
        from agent_hub_host import heavy_test_admission
        event = {'holder_pid': 123, 'waiter_pid': 124, 'process_group': 124,
                 'waiter_start': '456', 'ticket_sequence': 7, 'priority': 'normal',
                 'intake_id': '96c0a099-7844-44bb-acb0-2dfa8151634f'}
        dispatcher = SimpleNamespace(request=mock.Mock(side_effect=[
            {'task': {'version': 9}}, {'ok': True}]))
        current = {'at': 20, 'processes': {}}
        with patch.object(heavy_test_admission, 'reap_nested_holds',
                          return_value=(current, [event])), patch.object(session_host, '_log'):
            session_host._HEAVY_REAPER_SNAPSHOT = None
            session_host.heavy_test_reaper_step_once(dispatcher)
        self.assertIs(session_host._HEAVY_REAPER_SNAPSHOT, current)
        self.assertEqual(dispatcher.request.call_args_list[0].args,
                         ('GET', '/api/intake/tasks/96c0a099-7844-44bb-acb0-2dfa8151634f'))
        post = dispatcher.request.call_args_list[1]
        self.assertEqual(post.args[:2], ('POST', '/api/intake/tasks/96c0a099-7844-44bb-acb0-2dfa8151634f/events'))
        self.assertEqual(post.args[2]['expected_version'], 9)
        self.assertEqual(post.args[2]['event']['kind'], 'note')
        self.assertNotIn('intake_id', post.args[2]['event']['text'])

    def test_blocked_daemon_cannot_log_into_later_stdout(self):
        import contextlib
        import io
        import threading
        release, entered = threading.Event(), threading.Event()
        threads = []

        def slow_observer(current, collector):
            def step():
                session_host._log('observer_started')
                entered.set()
                release.wait(5)
                session_host._log('disk_cycle')
            thread = session_host._background_thread(target=step, daemon=True)
            threads.append(thread)
            thread.start()
            self.assertTrue(entered.wait(1))
            return thread

        try:
            with tempfile.TemporaryDirectory() as directory, patch.object(session_host.config,'load_config',return_value={'machine_id':'test-machine','access_token':'synthetic'}), patch.object(session_host.fleet,'load_settings',return_value={}), patch.object(session_host,'qualify_sender_binding',return_value=SimpleNamespace(session_id='test-native',workspace=str(pathlib.Path.cwd()))), patch.object(session_host.fleet,'Dispatcher'), patch.object(session_host.session_control,'execute'), patch.object(session_host,'maybe_start_disk_thread',side_effect=slow_observer), contextlib.redirect_stdout(io.StringIO()) as controller_stdout:
                session_host.run('synthetic-settings',directory,cycles=1)
                self.assertIn('observer_started', controller_stdout.getvalue())
                self.assertFalse(release.is_set())
                self.assertTrue(threads[0].is_alive())
            with contextlib.redirect_stdout(io.StringIO()) as later_stdout:
                release.set()
                threads[0].join(2)
            self.assertFalse(threads[0].is_alive())
            self.assertEqual(later_stdout.getvalue(), '')
        finally:
            release.set()
            for thread in threads:
                thread.join(2)

    def test_release_poller_error_line_after_exit_does_not_reach_stdout(self):
        # self_update.loop's error lines used plain print, so a poller still
        # inside a network call when run() returned wrote into whatever owned
        # stdout next (Host canary flake, intake 26fc40d3).
        import io
        import threading
        from contextlib import redirect_stdout
        gate = threading.Event()
        finished = threading.Event()

        def late_loop(request, stop, *, log=print, **kwargs):
            gate.wait(timeout=10)
            log('LATE-RELEASE-LINE', flush=True)
            finished.set()

        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, patch.object(session_host.config,'load_config',return_value={'machine_id':'test-machine','access_token':'synthetic'}), patch.object(session_host.fleet,'load_settings',return_value={}), patch.object(session_host,'qualify_sender_binding',return_value=SimpleNamespace(session_id='test-native',workspace=str(pathlib.Path.cwd()))), patch.object(session_host.fleet,'Dispatcher'), patch.object(session_host.session_control,'execute'), patch.object(session_host.self_update,'loop',late_loop), redirect_stdout(buf):
            session_host.run('synthetic-settings',directory,cycles=1)
            gate.set()
            self.assertTrue(finished.wait(timeout=5))
        self.assertNotIn('LATE-RELEASE-LINE', buf.getvalue())

    def test_unbound_controller_has_no_fleet_or_command_effects(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(session_host.config,'load_config',return_value={'machine_id':'test-machine','access_token':'synthetic'}), patch.object(session_host.fleet,'load_settings',return_value={}), patch.object(session_host,'qualify_sender_binding',side_effect=ValueError('unbound')), patch.object(session_host.fleet,'Dispatcher') as dispatcher, patch.object(session_host.session_control,'execute') as execute:
            with self.assertRaises(ValueError):session_host.run('synthetic-settings',directory,cycles=1)
            dispatcher.assert_not_called();execute.assert_not_called()

    def test_supervisor_identity_rejects_reused_tag_or_engine(self):
        path=pathlib.Path(__file__).resolve().parents[2]/'scripts/session-host-supervisor.py'
        spec=importlib.util.spec_from_file_location('host_supervisor',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        receipt=dict(id='synthetic-id',tag='controller',workspace='/synthetic',engine='shell',created_at_ms=1,command=['python','host'])
        self.assertTrue(module.same_controller(dict(receipt),receipt))
        for key,value in [('id','replacement'),('created_at_ms',2),('engine','opencode'),('command',['another','command'])]:
            changed=dict(receipt);changed[key]=value
            self.assertFalse(module.same_controller(changed,receipt))

    def test_supervisor_identity_ignores_observed_workspace_drift(self):
        # aplexer 0.1.23 moved a live Windows controller's workspace to %TEMP%
        # (DESKTOP-FP4OP26, intake bf0863c4); that is not a different controller.
        path=pathlib.Path(__file__).resolve().parents[2]/'scripts/session-host-supervisor.py'
        spec=importlib.util.spec_from_file_location('host_supervisor',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        receipt=dict(id='synthetic-id',tag='controller',workspace='/synthetic',engine='shell',created_at_ms=1,command=['python','host'])
        self.assertTrue(module.same_controller(dict(receipt,workspace='C:/Users/u/AppData/Local/Temp'),receipt))

    def test_windows_session_host_does_not_pin_drifting_sender_workspace(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(session_host, '_is_windows', return_value=True), \
             patch.object(session_host, 'ensure_private_journal'), \
             patch.object(session_host.config, 'load_config', return_value={'machine_id': 'test-machine', 'access_token': 'synthetic'}), \
             patch.object(session_host.fleet, 'load_settings', return_value={}), \
             patch.object(session_host, 'qualify_sender_binding', return_value=SimpleNamespace(session_id='test-native', workspace='C:\\Users\\u\\AppData\\Local\\Temp')) as qualify, \
             patch.object(session_host.fleet, 'Dispatcher'), \
             patch.object(session_host.session_control, 'execute') as execute:
            session_host.run('synthetic-settings', directory, cycles=1)
            qualify.assert_called_once_with('aplexer', None)
            self.assertIsNone(execute.call_args.args[0].sender_workspace)

    def test_session_host_loads_role_config_independently_of_disabled_consumer(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = pathlib.Path(directory)
            roles_file = journal / "roles.json"
            roles_file.write_text('{"version": 1, "enabled": true, "profiles": {"principal-wake": {"enabled": true}}}')

            with patch.object(session_host.config, 'load_config', return_value={'machine_id': 'test-machine', 'access_token': 'synthetic'}), \
                 patch.object(session_host.fleet, 'load_settings', return_value={'role_consumer': {'enabled': False}}), \
                 patch.object(session_host, 'qualify_sender_binding', return_value=SimpleNamespace(session_id='test-native', workspace=str(pathlib.Path.cwd()))), \
                 patch.object(session_host.fleet, 'Dispatcher') as dispatcher, \
                 patch.object(session_host, 'RoleConsumer') as role_consumer, \
                 patch.object(session_host.session_control, 'execute'):
                session_host.run('synthetic-settings', directory, cycles=1)

                dispatcher.assert_called_once()
                call_kw = dispatcher.call_args.kwargs
                self.assertEqual(call_kw.get('role_config_path'), str(roles_file))

                role_consumer.assert_called_once()
                rc_kw = role_consumer.call_args.kwargs
                self.assertEqual(rc_kw.get('config_path'), str(roles_file))
                self.assertFalse(rc_kw.get('enabled'))

    def test_session_host_runs_cleanly_when_roles_json_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(session_host.config, 'load_config', return_value={'machine_id': 'test-machine', 'access_token': 'synthetic'}), \
                 patch.object(session_host.fleet, 'load_settings', return_value={}), \
                 patch.object(session_host, 'qualify_sender_binding', return_value=SimpleNamespace(session_id='test-native', workspace=str(pathlib.Path.cwd()))), \
                 patch.object(session_host.fleet, 'Dispatcher') as dispatcher, \
                 patch.object(session_host, 'RoleConsumer') as role_consumer, \
                 patch.object(session_host.session_control, 'execute'):
                session_host.run('synthetic-settings', directory, cycles=1)

                dispatcher.assert_called_once()
                call_kw = dispatcher.call_args.kwargs
                self.assertIsNone(call_kw.get('role_config_path'))
                self.assertIsNone(call_kw.get('role_config'))

                role_consumer.assert_called_once()
                rc_kw = role_consumer.call_args.kwargs
                self.assertIsNone(rc_kw.get('config_path'))
                self.assertFalse(rc_kw.get('enabled'))

    def test_session_host_explicit_invalid_dict_fails_closed_without_journal_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = pathlib.Path(directory)
            roles_file = journal / "roles.json"
            roles_file.write_text('{"version": 1, "enabled": true, "profiles": {"principal-wake": {"enabled": true}}}')

            # Malformed explicit dict that causes load_role_config to fail/raise
            broken_cfg = {"version": 1, "enabled": True, "profiles": "invalid_not_a_dict"}

            with patch.object(session_host.config, 'load_config', return_value={'machine_id': 'test-machine', 'access_token': 'synthetic'}), \
                 patch.object(session_host.fleet, 'load_settings', return_value={'role_config': broken_cfg, 'role_consumer': {'enabled': False}}), \
                 patch.object(session_host, 'qualify_sender_binding', return_value=SimpleNamespace(session_id='test-native', workspace=str(pathlib.Path.cwd()))), \
                 patch.object(session_host.fleet, 'Dispatcher') as dispatcher, \
                 patch.object(session_host, 'RoleConsumer') as role_consumer, \
                 patch.object(session_host.session_control, 'execute'):
                session_host.run('synthetic-settings', directory, cycles=1)

                dispatcher.assert_called_once()
                call_kw = dispatcher.call_args.kwargs
                # Must fail closed: neither the broken object nor the journal fallback is used
                self.assertIsNone(call_kw.get('role_config_path'))
                self.assertIsNone(call_kw.get('role_config'))

                role_consumer.assert_called_once()
                rc_kw = role_consumer.call_args.kwargs
                self.assertIsNone(rc_kw.get('config_path'))
                self.assertIsNone(rc_kw.get('role_config'))
                self.assertFalse(rc_kw.get('enabled'))

    def test_session_host_passes_journal_dir_to_dispatcher(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(session_host.config, 'load_config', return_value={'machine_id': 'test-machine', 'access_token': 'synthetic'}), \
                 patch.object(session_host.fleet, 'load_settings', return_value={}), \
                 patch.object(session_host, 'qualify_sender_binding', return_value=SimpleNamespace(session_id='test-native', workspace=str(pathlib.Path.cwd()))), \
                 patch.object(session_host.fleet, 'Dispatcher') as dispatcher, \
                 patch.object(session_host, 'RoleConsumer'), \
                 patch.object(session_host.session_control, 'execute'):
                session_host.run('synthetic-settings', directory, cycles=1)

                dispatcher.assert_called_once()
                call_kw = dispatcher.call_args.kwargs
                self.assertEqual(call_kw.get('journal_dir'), directory)

    def _write_settings(self, path, workspaces, mtime):
        import json as _json
        import os as _os
        pathlib.Path(path).write_text(_json.dumps({
            "version": 1, "subscriptions": [], "workspaces": workspaces,
        }))
        # Explicit mtime so reload detection is deterministic on any fs granularity.
        _os.utime(path, (mtime, mtime))

    def test_reload_applies_new_workspaces_without_restart(self):
        import json as _json
        with tempfile.TemporaryDirectory() as directory:
            settings_file = str(pathlib.Path(directory) / "subscriptions.json")
            self._write_settings(settings_file, {"proj": directory}, 1792655000.0)
            first, mtime = session_host.maybe_reload_fleet_settings(settings_file, None)
            self.assertIsInstance(first, dict)
            self.assertIn("proj", first["workspaces"])
            # Same mtime: no reload.
            again, same_mtime = session_host.maybe_reload_fleet_settings(settings_file, mtime)
            self.assertIsNone(again)
            self.assertEqual(same_mtime, mtime)
            # Operator adds a repo: reload picks it up.
            self._write_settings(settings_file, {"proj": directory, "newrepo": directory}, 1792655100.0)
            reloaded, new_mtime = session_host.maybe_reload_fleet_settings(settings_file, mtime)
            self.assertIsNotNone(reloaded)
            self.assertIn("newrepo", reloaded["workspaces"])
            self.assertNotEqual(new_mtime, mtime)

    def test_reload_invalid_edit_fails_closed_on_last_good(self):
        import os as _os
        with tempfile.TemporaryDirectory() as directory:
            settings_file = str(pathlib.Path(directory) / "subscriptions.json")
            self._write_settings(settings_file, {"proj": directory}, 1792655000.0)
            _, mtime = session_host.maybe_reload_fleet_settings(settings_file, None)
            pathlib.Path(settings_file).write_text("{broken json")
            _os.utime(settings_file, (1792655100.0, 1792655100.0))
            kept, attempted = session_host.maybe_reload_fleet_settings(settings_file, mtime)
            self.assertIsNone(kept)
            self.assertNotEqual(attempted, mtime)
            # No spam on the next loop while the file is still broken.
            kept_again, _ = session_host.maybe_reload_fleet_settings(settings_file, attempted)
            self.assertIsNone(kept_again)

    def test_reload_missing_file_keeps_running(self):
        missing = str(pathlib.Path(tempfile.gettempdir()) / "hub-no-such-settings-1792655000.json")
        kept, mtime = session_host.maybe_reload_fleet_settings(missing, None)
        self.assertIsNone(kept)
        self.assertIsNone(mtime)
        kept, mtime = session_host.maybe_reload_fleet_settings(None, 1234.0)
        self.assertIsNone(kept)
        self.assertEqual(mtime, 1234.0)

    def test_run_swaps_dispatcher_settings_on_edit(self):
        import json as _json
        with tempfile.TemporaryDirectory() as directory:
            settings_file = str(pathlib.Path(directory) / "subscriptions.json")
            self._write_settings(settings_file, {"proj": directory}, 1792655000.0)
            v1 = {"version": 1, "subscriptions": [], "workspaces": {"proj": directory}}
            v2 = {"version": 1, "subscriptions": [], "workspaces": {"proj": directory, "newrepo": directory}}
            with patch.object(session_host.config, 'load_config', return_value={'machine_id': 'test-machine', 'access_token': 'synthetic'}), \
                 patch.object(session_host.fleet, 'load_settings', side_effect=[v1, v2]) as load_settings, \
                 patch.object(session_host, 'fleet_settings_mtime', side_effect=[1000.0, 2000.0]) as mtimes, \
                 patch.object(session_host, 'qualify_sender_binding', return_value=SimpleNamespace(session_id='test-native', workspace=str(pathlib.Path.cwd()))), \
                 patch.object(session_host.fleet, 'Dispatcher') as dispatcher, \
                 patch.object(session_host, 'RoleConsumer'), \
                 patch.object(session_host, 'offers_loop', lambda *a, **k: None), \
                 patch.object(session_host, 'maybe_start_maintenance_thread', return_value=None), \
                 patch.object(session_host, 'maybe_start_token_thread', return_value=None), \
                 patch.object(session_host.session_control, 'execute'):
                # The real offers loop raises offers_polling while it polls
                # (which, by design, defers the swap) and background steps
                # may consume the second load_settings value; keep them out
                # so the swap is deterministic.
                session_host.run(settings_file, directory, cycles=1)
                instance = dispatcher.return_value
                self.assertEqual(instance.settings, v2)
                self.assertEqual(load_settings.call_count, 2)

