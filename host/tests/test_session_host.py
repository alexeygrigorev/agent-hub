import importlib.util
import pathlib
import tempfile
import unittest
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

